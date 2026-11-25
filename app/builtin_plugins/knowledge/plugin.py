"""knowledge.provider 的内置实现：SQLite FTS5 + 带坐标 chunk（Phase 0 §2.1）。

- 索引由应用侧双写（无 external-content 触发器依赖）；FTS5 不可用时建同构
  普通表并切 LIKE 查询，mode 置 DEGRADED（由内核 service 写审计，不静默）。
- 文档检索必须带 allowed_paths：路径经 resolve 后逐条比对白名单，禁止越界检索。
- 消息检索按服务端派生的 user_id 限会话属主，不暴露跨用户消息。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.orm import Session

from app.builtin_plugins.knowledge.vector_store import LocalVectorStore
from app.builtin_plugins.manifest import PluginManifest
from app.config import get_settings
from app.core.errors import ApiError
from app.core.ids import utc_now
from app.kernel.ports.knowledge_provider import KnowledgeChunk, KnowledgeProvider, ParsedFile
from app.models import KnowledgeChunk as ChunkRow
from app.models import KnowledgeDoc as DocRow


def ensure_schema(bind: Any) -> bool:
    """装配期建 FTS5 虚表与触发器（迁移已建则幂等跳过）；不支持 FTS5 返回 False。

    单机 exe 的启动自检走这里：不假设目标 sqlite3 有 FTS5，缺失时保持
    LIKE 降级路径并把 mode 置 DEGRADED，由内核 service 写审计。
    """
    if getattr(bind, "dialect", None) is None or bind.dialect.name != "sqlite":
        return False
    has_fts5 = probe_fts5()
    with bind.begin() as connection:
        if has_fts5:
            connection.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS zr_knowledge_fts USING fts5(tenant_id, chunk_id, text, tokenize='unicode61')"
            )
        else:
            # FTS5 缺失时也必须创建同构普通表，否则 LIKE 降级路径没有索引表可查。
            connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS zr_knowledge_fts (tenant_id VARCHAR(36), chunk_id VARCHAR(36), text TEXT)"
            )
    return has_fts5


def probe_fts5() -> bool:
    """运行期探测目标 Python 的 sqlite3 是否支持 FTS5（exe 打包必须实测）。"""
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE _probe USING fts5(text)")
            connection.execute("DROP TABLE _probe")
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True


def _path_allowed(path: str, allowed_paths: list[str] | None) -> bool:
    if not allowed_paths:
        return False  # fail-closed：无白名单 = 不返回任何片段
    resolved = Path(path).expanduser().resolve()
    return any(
        resolved == root or root in resolved.parents
        for root in (Path(item).expanduser().resolve() for item in allowed_paths)
    )


def _fts_query(query: str) -> str:
    terms = [term for term in query.split() if term]
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms) or '""'


def _cjk_fallback_terms(query: str) -> list[str]:
    """为 unicode61 不做中文分词的场景生成有界三字片段，供 LIKE 补召回。"""
    runs = re.findall(r"[\u3400-\u9fff]+", query)
    terms: list[str] = []
    for run in runs:
        if len(run) >= 3:
            terms.extend(run[index:index + 3] for index in range(len(run) - 2))
        elif run:
            terms.append(run)
    stop_terms = {"帮我分", "我分析", "分析一", "析一下", "请帮我", "如何", "怎么", "一下", "请问"}
    unique = [term for term in dict.fromkeys(terms) if term not in stop_terms]
    return unique[-24:]


class SqliteKnowledgeProvider(KnowledgeProvider):
    def __init__(self, *, fts5: bool | None = None) -> None:
        self.fts5 = probe_fts5() if fts5 is None else fts5
        self.last_degradation_reason: str | None = None if self.fts5 else "sqlite3 FTS5 is unavailable; LIKE fallback in use"

    def mode(self) -> str:
        return "AVAILABLE" if self.fts5 and self.last_degradation_reason is None else "DEGRADED"

    def index_file(self, db: Session, *, tenant_id: UUID, file_id: UUID, path: str, sha256: str, parsed: ParsedFile, size_bytes: int) -> dict:
        existing = db.scalar(select(DocRow).where(DocRow.tenant_id == tenant_id, DocRow.file_id == file_id))
        if existing is not None:
            self._delete_doc(db, existing)
        # 总入库字节上限：超限丢弃后续 chunk 并标 truncated（由内核 service 写审计）
        max_total = get_settings().knowledge_max_total_bytes
        kept: list = []
        total = 0
        truncated = parsed.truncated
        for block in parsed.blocks:
            block_bytes = len(block.text.encode("utf-8"))
            if total + block_bytes > max_total:
                truncated = True
                break
            kept.append(block)
            total += block_bytes
        doc = DocRow(
            tenant_id=tenant_id, file_id=file_id, path=path, sha256=sha256,
            status="ACTIVE", truncated=int(truncated), chunk_count=len(kept),
            size_bytes=size_bytes, indexed_at=utc_now(),
        )
        db.add(doc)
        db.flush()
        chunks = [
            ChunkRow(
                tenant_id=tenant_id, doc_id=doc.id, seq=seq, text=block.text,
                page_no=block.page_no, sheet=block.sheet, row_no=block.row_no, col_no=block.col_no,
                line_start=block.line_start, line_end=block.line_end,
                token_count=max(1, len(block.text) // 2),
            )
            for seq, block in enumerate(kept)
        ]
        if chunks:
            db.add_all(chunks)
            db.flush()
            # FTS5/普通索引表是 SQLite 本地实现；其他方言直接从 chunk 表做受限 LIKE 检索，
            # 不依赖 SQLite 隐式 rowid，也不要求企业数据库额外建一张私有 FTS 表。
            if db.get_bind().dialect.name == "sqlite":
                chunk_ids = [str(chunk.id) for chunk in chunks]
                rowids = db.execute(
                    text("SELECT id, rowid FROM zr_knowledge_chunk WHERE id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": chunk_ids},
                ).all()
                rowid_by_id = {str(row.id): row.rowid for row in rowids}
                db.execute(
                    text("INSERT INTO zr_knowledge_fts(rowid, tenant_id, chunk_id, text) VALUES (:rowid, :tenant, :cid, :text)"),
                    [
                        {"rowid": rowid_by_id[str(chunk.id)], "tenant": str(tenant_id), "cid": str(chunk.id), "text": chunk.text}
                        for chunk in chunks
                        if str(chunk.id) in rowid_by_id
                    ],
                )
        return {"doc_id": str(doc.id), "chunks": len(kept), "truncated": truncated, "mode": self.mode()}

    def _delete_doc(self, db: Session, doc: DocRow) -> None:
        # SQLite FTS5/LIKE 索引表与 chunk 双写；其他方言直接从 chunk 表检索，不维护 rowid 索引。
        if db.get_bind().dialect.name == "sqlite":
            db.execute(
                text(
                    "DELETE FROM zr_knowledge_fts WHERE rowid IN "
                    "(SELECT rowid FROM zr_knowledge_chunk WHERE doc_id = :doc_id)"
                ),
                {"doc_id": str(doc.id)},
            )
        db.execute(delete(ChunkRow).where(ChunkRow.doc_id == doc.id))
        db.delete(doc)
        db.flush()

    def delete_file(self, db: Session, *, tenant_id: UUID, file_id: UUID) -> None:
        docs = db.scalars(select(DocRow).where(DocRow.tenant_id == tenant_id, DocRow.file_id == file_id)).all()
        for doc in docs:
            self._delete_doc(db, doc)

    def reindex(self, db: Session, *, tenant_id: UUID) -> dict:
        """存量回填：按 zr_file 重新解析 PARSED 文件（路径按 storage_key 还原）。"""
        from app.models import File
        from app.services.file_parser import parse_file_structured

        settings = get_settings()
        rows = db.scalars(select(File).where(File.tenant_id == tenant_id, File.status == "PARSED")).all()
        indexed = 0
        skipped = 0
        for item in rows:
            path = Path(settings.file_storage_dir) / item.storage_key
            if not path.is_file():
                skipped += 1
                continue
            try:
                parsed = parse_file_structured(path, Path(item.original_name).suffix, settings.file_max_extracted_text, max_blocks=settings.knowledge_max_chunks_per_file, max_block_chars=settings.knowledge_chunk_max_chars)
                self.index_file(db, tenant_id=tenant_id, file_id=item.id, path=str(path), sha256=item.sha256, parsed=parsed, size_bytes=item.size_bytes)
                indexed += 1
            except Exception:  # noqa: BLE001 - 单个存量文件失败不影响整体回填
                skipped += 1
        return {"indexed": indexed, "skipped": skipped, "mode": self.mode()}

    def search(self, *, tenant_id: UUID, query: str, top_k: int = 5, filters: dict | None = None, db: Any = None) -> list[KnowledgeChunk]:
        if db is None:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "local knowledge search requires a DB session", 503)
        return self.search_db(db, tenant_id=tenant_id, query=query, top_k=top_k, filters=filters)

    def search_db(self, db: Session, *, tenant_id: UUID, query: str, top_k: int = 5, filters: dict | None = None) -> list[KnowledgeChunk]:
        """真实检索入口：FTS5 MATCH（bm25 排序）或 LIKE 降级，强制 allowed_paths。"""
        allowed = list((filters or {}).get("allowed_paths") or [])
        if not query.strip() or not allowed:
            return []
        limit = max(1, min(int(top_k), 50))
        is_sqlite = db.get_bind().dialect.name == "sqlite"
        if not is_sqlite:
            self.last_degradation_reason = "non-SQLite dialect; using chunk-table LIKE retrieval"
        if is_sqlite and self.fts5:
            rows = db.execute(
                text(
                    "SELECT c.id, c.text, c.page_no, c.sheet, c.row_no, c.col_no, c.line_start, c.line_end, "
                    "d.path AS path, d.file_id AS file_id, bm25(zr_knowledge_fts) AS score "
                    "FROM zr_knowledge_fts JOIN zr_knowledge_chunk c ON c.id = zr_knowledge_fts.chunk_id "
                    "JOIN zr_knowledge_doc d ON d.id = c.doc_id "
                    "WHERE zr_knowledge_fts MATCH :query AND zr_knowledge_fts.tenant_id = :tenant_id "
                    "AND d.tenant_id = :tenant_id AND d.status = 'ACTIVE' "
                    "AND d.path IN :allowed_paths "
                    "ORDER BY score LIMIT :limit"
                ).bindparams(bindparam("allowed_paths", expanding=True)),
                {"query": _fts_query(query), "tenant_id": str(tenant_id), "allowed_paths": allowed, "limit": limit},
            ).mappings().all()
        elif is_sqlite:
            pattern = f"%{query.strip()}%"
            rows = db.execute(
                text(
                    "SELECT c.id, c.text, c.page_no, c.sheet, c.row_no, c.col_no, c.line_start, c.line_end, "
                    "d.path AS path, d.file_id AS file_id, 0.0 AS score "
                    "FROM zr_knowledge_fts f JOIN zr_knowledge_chunk c ON c.id = f.chunk_id "
                    "JOIN zr_knowledge_doc d ON d.id = c.doc_id "
                    "WHERE f.tenant_id = :tenant_id AND f.text LIKE :pattern "
                    "AND d.tenant_id = :tenant_id AND d.status = 'ACTIVE' "
                    "AND d.path IN :allowed_paths "
                    "ORDER BY c.seq LIMIT :limit"
                ).bindparams(bindparam("allowed_paths", expanding=True)),
                {"pattern": pattern, "tenant_id": str(tenant_id), "allowed_paths": allowed, "limit": limit},
            ).mappings().all()
        else:
            pattern = f"%{query.strip()}%"
            rows = db.execute(
                text(
                    "SELECT c.id, c.text, c.page_no, c.sheet, c.row_no, c.col_no, c.line_start, c.line_end, "
                    "d.path AS path, d.file_id AS file_id, 0.0 AS score "
                    "FROM zr_knowledge_chunk c JOIN zr_knowledge_doc d ON d.id = c.doc_id "
                    "WHERE c.tenant_id = :tenant_id AND c.text LIKE :pattern "
                    "AND d.tenant_id = :tenant_id AND d.status = 'ACTIVE' "
                    "AND d.path IN :allowed_paths "
                    "ORDER BY c.seq LIMIT :limit"
                ).bindparams(bindparam("allowed_paths", expanding=True)),
                {"pattern": pattern, "tenant_id": str(tenant_id), "allowed_paths": allowed, "limit": limit},
            ).mappings().all()
        # unicode61 将连续汉字视为一个 token，整句提问往往无法命中只包含主题词的文档。
        # 当 FTS/完整短语命中不足时，用有界 CJK 三字片段补召回；仍在 SQL 层先过滤授权路径。
        if len(rows) < limit:
            terms = _cjk_fallback_terms(query)
            if terms:
                match_conditions = [
                    f"c.text LIKE :cjk_{index} ESCAPE '\\'"
                    for index in range(len(terms))
                ]
                score_terms = [
                    f"CASE WHEN c.text LIKE :cjk_{index} ESCAPE '\\' THEN 1 ELSE 0 END"
                    for index in range(len(terms))
                ]
                params: dict[str, Any] = {
                    "tenant_id": str(tenant_id),
                    "allowed_paths": allowed,
                    "limit": limit - len(rows),
                }
                for index, term in enumerate(terms):
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    params[f"cjk_{index}"] = f"%{escaped}%"
                existing_ids = [str(row["id"]) for row in rows]
                exclusion = ""
                bind_params = [bindparam("allowed_paths", expanding=True)]
                if existing_ids:
                    exclusion = "AND c.id NOT IN :existing_ids "
                    params["existing_ids"] = existing_ids
                    bind_params.append(bindparam("existing_ids", expanding=True))
                if is_sqlite:
                    from_clause = "FROM zr_knowledge_fts f JOIN zr_knowledge_chunk c ON c.id = f.chunk_id "
                    tenant_clause = "f.tenant_id = :tenant_id"
                else:
                    from_clause = "FROM zr_knowledge_chunk c "
                    tenant_clause = "c.tenant_id = :tenant_id"
                fallback_sql = (
                    "SELECT c.id, c.text, c.page_no, c.sheet, c.row_no, c.col_no, c.line_start, c.line_end, "
                    "d.path AS path, d.file_id AS file_id, "
                    f"-({' + '.join(score_terms)}) AS score "
                    + from_clause
                    + "JOIN zr_knowledge_doc d ON d.id = c.doc_id "
                    + f"WHERE {tenant_clause} AND d.tenant_id = :tenant_id "
                    + "AND d.status = 'ACTIVE' AND d.path IN :allowed_paths "
                    + f"{exclusion}AND ({' OR '.join(match_conditions)}) "
                    + "ORDER BY score LIMIT :limit"
                )
                fallback_rows = db.execute(
                    text(fallback_sql).bindparams(*bind_params),
                    params,
                ).mappings().all()
                rows = [*rows, *fallback_rows]
        results: list[KnowledgeChunk] = []
        for row in rows:
            if not _path_allowed(row["path"], allowed):
                continue
            results.append(
                KnowledgeChunk(
                    source=row["path"], content=row["text"], score=float(row["score"]),
                    metadata={"chunk_id": str(row["id"]), "file_id": str(row["file_id"]), "page_no": row["page_no"], "sheet": row["sheet"], "row_no": row["row_no"], "col_no": row["col_no"], "line_start": row["line_start"], "line_end": row["line_end"]},
                )
            )
        return results

    def search_messages(self, db: Any, *, tenant_id: UUID, user_id: UUID | None, query: str, top_k: int = 5, conversation_id: UUID | None = None) -> list[KnowledgeChunk]:
        return self.search_messages_db(
            db, tenant_id=tenant_id, user_id=user_id, query=query,
            top_k=top_k, conversation_id=conversation_id,
        )

    def search_messages_db(
        self, db: Session, *, tenant_id: UUID, user_id: UUID | None, query: str,
        top_k: int = 5, conversation_id: UUID | None = None,
    ) -> list[KnowledgeChunk]:
        """消息级全文检索：只查当前用户拥有的会话，不依赖 FTS5（短文本 LIKE 足够）。"""
        text_value = query.strip()
        if not text_value or user_id is None:
            return []
        escaped = text_value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conversation_filter = "AND m.conversation_id = :conv " if conversation_id is not None else ""
        params = {
            "tenant": str(tenant_id),
            "user": str(user_id),
            "pattern": f"%{escaped}%",
            "limit": max(1, min(int(top_k), 50)),
        }
        if conversation_id is not None:
            params["conv"] = str(conversation_id)
        rows = db.execute(
            text(
                "SELECT m.id, m.conversation_id, m.sender_type, m.content, m.created_at "
                "FROM zr_message m JOIN zr_conversation c ON c.id = m.conversation_id "
                "WHERE m.tenant_id = :tenant AND c.owner_user_id = :user "
                "AND m.content LIKE :pattern ESCAPE '\\' "
                + conversation_filter
                + "ORDER BY m.created_at DESC LIMIT :limit"
            ),
            params,
        ).mappings().all()
        return [
            KnowledgeChunk(
                source=f"message:{row['id']}", content=row["content"], score=0.0,
                metadata={
                    "source_type": "message", "message_id": str(row["id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "sender_type": row["sender_type"],
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                },
            )
            for row in rows
        ]


class KnowledgePlugin:
    manifest = PluginManifest(
        name="knowledge",
        description="本地知识库：FTS5 全文检索 + 持久化本地向量存储",
        provides=("knowledge.provider", "vector.store"),
        permissions=("file.read", "db.query"),
    )

    def contribute(self, sdk: Any) -> None:
        fts5_available = ensure_schema(sdk.engine)
        provider = SqliteKnowledgeProvider(fts5=fts5_available)
        if sdk.engine.dialect.name != "sqlite":
            provider.last_degradation_reason = "non-SQLite dialect; using chunk-table LIKE retrieval"
        sdk.publish("knowledge.provider", provider)
        label = "builtin_sqlite_fts5" if fts5_available else "builtin_like_fallback"
        sdk.register_port("knowledge.provider", lambda: provider, label=label)
        vector_store = LocalVectorStore(sdk.session)
        sdk.register_port("vector.store", lambda: vector_store, label="builtin_local_exact_cosine")
