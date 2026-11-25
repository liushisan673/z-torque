"""knowledge.provider 的内置实现：SQLite FTS5 + 带坐标 chunk（Phase 0 §2.1）。

- 索引由应用侧双写（无 external-content 触发器依赖）；FTS5 不可用时建同构
  普通表并切 LIKE 查询，mode 置 DEGRADED（由内核 service 写审计，不静默）。
- 文档检索必须带 allowed_paths：路径经 resolve 后逐条比对白名单，禁止越界检索。
- 消息检索按服务端派生的 user_id 限会话属主，不暴露跨用户消息。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

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
    if getattr(bind, "dialect", None) is None or bind.dialect.name != "sqlite" or not probe_fts5():
        return False
    with bind.begin() as connection:
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS zr_knowledge_fts USING fts5(tenant_id UNINDEXED, chunk_id UNINDEXED, text, tokenize='trigram')"
        )
    return True


def probe_fts5() -> bool:
    """运行期探测目标 Python 的 sqlite3 是否支持 FTS5（exe 打包必须实测）。"""
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE _probe USING fts5(text, tokenize='trigram')")
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


_CJK_RUN = re.compile(r"[\\u3400-\\u9fff\\uf900-\\ufaff]+")
_WORD = re.compile(r"[A-Za-z0-9_]{2,}")


def _query_terms(query: str, *, limit: int = 24) -> list[str]:
    """为 FTS5 trigram 构造跨中英文的候选词；中文无空格句子拆为重叠三元组。"""
    normalized = unicodedata.normalize("NFKC", query).strip()
    terms: list[str] = []
    for run in _CJK_RUN.findall(normalized):
        if len(run) < 3:
            terms.append(run)
        else:
            terms.extend(run[index:index + 3] for index in range(len(run) - 2))
    terms.extend(word.lower() for word in _WORD.findall(normalized))
    # 保留出现顺序，去重后限制 MATCH 表达式大小，避免长提示词生成超大查询。
    return list(dict.fromkeys(term for term in terms if term))[:limit]


def _fts_query(query: str) -> str:
    terms = [term for term in _query_terms(query) if len(term) >= 3]
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


class SqliteKnowledgeProvider(KnowledgeProvider):
    def __init__(self, *, fts5: bool | None = None) -> None:
        self.fts5 = probe_fts5() if fts5 is None else fts5
        self.last_degradation_reason: str | None = None if self.fts5 else "sqlite3 FTS5 is unavailable; LIKE fallback in use"

    def mode(self) -> str:
        return "AVAILABLE" if self.fts5 else "DEGRADED"

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
            # 一次批量 flush + 一条 INSERT..SELECT，避免每个 chunk 两次 DB 往返；
            # FTS rowid 由索引表自行分配，chunk_id 是跨 SQLite/PostgreSQL 的稳定关联键。
            db.add_all(chunks)
            db.flush()
            db.execute(
                text(
                    "INSERT INTO zr_knowledge_fts(tenant_id, chunk_id, text) "
                    "SELECT CAST(tenant_id AS VARCHAR(36)), CAST(id AS VARCHAR(36)), text FROM zr_knowledge_chunk WHERE doc_id = :doc_id"
                ),
                {"doc_id": str(doc.id)},
            )
        return {"doc_id": str(doc.id), "chunks": len(kept), "truncated": truncated, "mode": self.mode()}

    def _delete_doc(self, db: Session, doc: DocRow) -> None:
        # 应用侧双写：FTS 与 chunk 同生共死（FTS5/普通表都按 rowid 删除）
        # 按稳定 chunk_id 批量删除，避免逐 chunk SELECT rowid + DELETE 两次往返；
        # SQLite FTS5 与普通回退表、PostgreSQL 普通表均支持该键。
        db.execute(
            text(
                "DELETE FROM zr_knowledge_fts "
                "WHERE chunk_id IN (SELECT CAST(id AS VARCHAR(36)) FROM zr_knowledge_chunk WHERE doc_id = :doc_id)"
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
                # 单文件保存点：一个坏文件回滚自身写入，不把 Session 留在 failed transaction 状态。
                with db.begin_nested():
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
        """真实检索入口：trigram FTS5 + BM25，或带命中计分的 LIKE 降级。

        owner_user_id 在 SQL 层与文件表联接后过滤，避免先 LIMIT 再做 Python 白名单过滤
        导致相关文档被其他用户的高排名文档挤出候选集。
        """
        options = filters or {}
        owner_user_id = options.get("owner_user_id")
        allowed = list(options.get("allowed_paths") or [])
        if not query.strip() or (owner_user_id is None and not allowed):
            return []
        limit = max(1, min(int(top_k), 50))
        params: dict[str, Any] = {"tenant_id": str(tenant_id), "limit": limit}
        joins = " JOIN zr_knowledge_chunk c ON CAST(c.id AS VARCHAR(36)) = zr_knowledge_fts.chunk_id JOIN zr_knowledge_doc d ON d.id = c.doc_id"
        clauses = [
            "zr_knowledge_fts.tenant_id = :tenant_id",
            "CAST(d.tenant_id AS VARCHAR(36)) = :tenant_id",
            "d.status = 'ACTIVE'",
        ]
        if owner_user_id is not None:
            joins += " JOIN zr_file scoped_file ON scoped_file.id = d.file_id AND scoped_file.tenant_id = d.tenant_id"
            clauses.extend([
                "CAST(scoped_file.owner_user_id AS VARCHAR(36)) = :owner_user_id",
                "scoped_file.status = 'PARSED'",
            ])
            params["owner_user_id"] = str(owner_user_id)
        else:
            path_params: list[str] = []
            for index, path in enumerate(allowed):
                key = f"path_{index}"
                params[key] = str(Path(path).expanduser().resolve())
                path_params.append(f":{key}")
            if not path_params:
                return []
            clauses.append(f"d.path IN ({', '.join(path_params)})")

        file_id = options.get("file_id")
        if file_id is not None:
            clauses.append("CAST(d.file_id AS VARCHAR(36)) = :file_id")
            params["file_id"] = str(file_id)
        excluded = [str(item) for item in (options.get("exclude_file_ids") or [])]
        if excluded:
            names: list[str] = []
            for index, value in enumerate(excluded):
                key = f"exclude_{index}"
                params[key] = value
                names.append(f":{key}")
            clauses.append(f"d.file_id NOT IN ({', '.join(names)})")

        where = " AND ".join(clauses)
        fts_expression = _fts_query(query)
        if self.fts5 and fts_expression:
            params["query"] = fts_expression
            rows = db.execute(
                text(
                    "SELECT c.id, c.text, c.page_no, c.sheet, c.row_no, c.col_no, c.line_start, c.line_end, "
                    "d.path AS path, d.file_id AS file_id, bm25(zr_knowledge_fts) AS score "
                    f"FROM zr_knowledge_fts{joins} WHERE zr_knowledge_fts MATCH :query AND {where} "
                    "ORDER BY score LIMIT :limit"
                ),
                params,
            ).mappings().all()
        else:
            terms = _query_terms(query) or [query.strip()]
            like_clauses: list[str] = []
            score_terms: list[str] = []
            for index, term in enumerate(terms):
                key = f"term_{index}"
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params[key] = f"%{escaped}%"
                like_clauses.append(f"zr_knowledge_fts.text LIKE :{key} ESCAPE '\\'")
                score_terms.append(f"(CASE WHEN zr_knowledge_fts.text LIKE :{key} ESCAPE '\\' THEN 1 ELSE 0 END)")
            score = " + ".join(score_terms)
            rows = db.execute(
                text(
                    "SELECT c.id, c.text, c.page_no, c.sheet, c.row_no, c.col_no, c.line_start, c.line_end, "
                    f"d.path AS path, d.file_id AS file_id, -({score}) AS score "
                    f"FROM zr_knowledge_fts{joins} WHERE {' OR '.join(like_clauses)} AND {where} "
                    "ORDER BY score, c.seq LIMIT :limit"
                ),
                params,
            ).mappings().all()

        results: list[KnowledgeChunk] = []
        for row in rows:
            # owner_user_id 已在 SQL 层校验；无用户上下文的内部调用仍必须逐条检查路径白名单。
            if owner_user_id is None and not _path_allowed(row["path"], allowed):
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
        rows = db.execute(
            text(
                "SELECT m.id, m.conversation_id, m.sender_type, m.content, m.created_at "
                "FROM zr_message m JOIN zr_conversation c ON c.id = m.conversation_id "
                "WHERE m.tenant_id = :tenant AND c.owner_user_id = :user "
                "AND m.content LIKE :pattern ESCAPE '\\' "
                "AND (:conv IS NULL OR m.conversation_id = :conv) "
                "ORDER BY m.created_at DESC LIMIT :limit"
            ),
            {
                "tenant": str(tenant_id), "user": str(user_id),
                "pattern": f"%{escaped}%",
                "conv": str(conversation_id) if conversation_id is not None else None,
                "limit": max(1, min(int(top_k), 50)),
            },
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
        description="本地知识库：SQLite FTS5 trigram 检索 + BM25 排序（不可用时 LIKE 降级）",
        provides=("knowledge.provider",),
        permissions=("file.read", "db.query"),
    )

    def contribute(self, sdk: Any) -> None:
        # 检索模式必须与实际数据库方言/FTS tokenizer 一致；不能按宿主 Python 的 sqlite3
        # 能力误判 PostgreSQL 目标库也支持 FTS5。
        fts5 = ensure_schema(sdk.engine)
        provider = SqliteKnowledgeProvider(fts5=fts5)
        sdk.publish("knowledge.provider", provider)
        sdk.register_port("knowledge.provider", lambda: provider, label="builtin_sqlite_fts5" if provider.fts5 else "builtin_sqlite_like_degraded")
