"""Generate the sanitized seed snapshot bundled with the Windows desktop package.

The snapshot keeps business data (agents, skills, knowledge bases, files, conversations)
but removes every deployer-owned credential. The user configures a fresh model key on
first launch through the onboarding dialog.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data"
DEFAULT_TARGET = ROOT / "seed-data"

_SENSITIVE_KEY = re.compile(r"(api[_-]?key|secret|token|password|authorization|access[_-]?key)", re.I)
_COUNT_TABLES = (
    "zr_agent",
    "zr_agent_version",
    "zr_skill",
    "zr_skill_version",
    "zr_knowledge_base",
    "zr_knowledge_doc",
    "zr_knowledge_chunk",
    "zr_model_provider",
    "zr_model",
    "zr_user",
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}


def _clean_config(value: object) -> object:
    if isinstance(value, dict):
        return {key: _clean_config(item) for key, item in value.items() if not _SENSITIVE_KEY.search(str(key))}
    if isinstance(value, list):
        return [_clean_config(item) for item in value]
    return value


def _scrub(conn: sqlite3.Connection) -> dict[str, int]:
    existing = _tables(conn)
    cleared: dict[str, int] = {}

    def clear(table: str, column: str) -> None:
        if table not in existing:
            return
        cursor = conn.execute(f"update {table} set {column} = NULL where {column} is not null and {column} != ''")
        cleared[f"{table}.{column}"] = cursor.rowcount

    clear("zr_model_provider", "secret_ref")
    clear("zr_connector", "secret_ref")
    clear("zr_inbound_webhook", "secret_ciphertext")
    clear("zr_inbound_webhook", "reply_token_ciphertext")

    # A bundled session must never let a new user inherit the deployer's login.
    if "zr_auth_session" in existing:
        cursor = conn.execute("delete from zr_auth_session")
        cleared["zr_auth_session.rows"] = cursor.rowcount

    for table in ("zr_model_provider", "zr_model", "zr_model_endpoint", "zr_connector"):
        if table not in existing:
            continue
        changed = 0
        for row_id, raw in conn.execute(f"select id, config from {table} where config is not null").fetchall():
            try:
                value = json.loads(raw) if isinstance(raw, str) else None
            except (TypeError, ValueError):
                value = None
            if value is None:
                continue
            cleaned = _clean_config(value)
            if cleaned != value:
                conn.execute(
                    f"update {table} set config = ? where id = ?",
                    (json.dumps(cleaned, ensure_ascii=False), row_id),
                )
                changed += 1
        cleared[f"{table}.config"] = changed

    return cleared


def _bind_bundled_model(conn: sqlite3.Connection) -> tuple[str, int]:
    """把内置模型标识写进所有已发布智能体快照，保证对话链路有模型可用。"""
    tables = _tables(conn)
    if "zr_model" not in tables or "zr_agent_version" not in tables:
        return "", 0
    row = conn.execute(
        "select model_identifier from zr_model where status = 'ENABLED' order by created_at limit 1"
    ).fetchone()
    if not row or not row[0]:
        return "", 0
    model = str(row[0])
    changed = 0
    versions = conn.execute(
        "select id, config_snapshot from zr_agent_version where status = 'PUBLISHED' and config_snapshot is not null"
    ).fetchall()
    for version_id, raw in versions:
        try:
            snapshot = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, ValueError):
            snapshot = None
        if not isinstance(snapshot, dict) or snapshot.get("model"):
            continue
        snapshot["model"] = model
        skill_ids = [
            str(item[0])
            for item in conn.execute(
                "select skill_version_id from zr_agent_version_skill where agent_version_id = ?",
                (version_id,),
            )
        ]
        tool_ids: list[str] = []
        if skill_ids:
            placeholders = ",".join("?" for _ in skill_ids)
            tool_ids = [
                str(item[0])
                for item in conn.execute(
                    f"select distinct tool_version_id from zr_skill_version_tool where skill_version_id in ({placeholders})",
                    skill_ids,
                )
            ]
        payload = {
            "config_snapshot": snapshot,
            "skill_version_ids": sorted(skill_ids),
            "tool_version_ids": sorted(tool_ids),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        conn.execute(
            "update zr_agent_version set config_snapshot = ?, snapshot_hash = ? where id = ?",
            (json.dumps(snapshot, ensure_ascii=False), digest, version_id),
        )
        changed += 1
    return model, changed


def _normalize_knowledge_paths(conn: sqlite3.Connection) -> int:
    """把开发机遗留的相对路径改成文件 storage_key，避免打包后路径白名单失配。

    检索层同时接受文件键与绝对路径；种子库统一存文件键可在任意数据目录下命中。
    """
    tables = _tables(conn)
    if "zr_knowledge_doc" not in tables or "zr_file" not in tables:
        return 0
    cursor = conn.execute(
        "update zr_knowledge_doc set path = ("
        "  select f.storage_key from zr_file f where f.id = zr_knowledge_doc.file_id"
        ") where exists (select 1 from zr_file f where f.id = zr_knowledge_doc.file_id) "
        "and path != (select f.storage_key from zr_file f where f.id = zr_knowledge_doc.file_id)"
    )
    return cursor.rowcount


def _bind_all_knowledge_bases(conn: sqlite3.Connection) -> tuple[list[str], int]:
    """把种子库里的全部知识库绑定给每个已发布智能体，首次打开即可检索。"""
    tables = _tables(conn)
    if "zr_knowledge_base" not in tables or "zr_agent_version" not in tables:
        return [], 0
    kb_ids = [
        str(row[0])
        for row in conn.execute(
            "select id from zr_knowledge_base where status = 'ACTIVE' order by created_at, id"
        )
    ]
    if not kb_ids:
        return [], 0
    changed = 0
    versions = conn.execute(
        "select id, config_snapshot from zr_agent_version where status = 'PUBLISHED'"
    ).fetchall()
    for version_id, raw in versions:
        try:
            snapshot = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, ValueError):
            snapshot = None
        if not isinstance(snapshot, dict):
            continue
        if snapshot.get("knowledge_base_ids") == kb_ids:
            continue
        snapshot["knowledge_base_ids"] = kb_ids
        skill_ids = [
            str(item[0])
            for item in conn.execute(
                "select skill_version_id from zr_agent_version_skill where agent_version_id = ?",
                (version_id,),
            )
        ]
        tool_ids: list[str] = []
        if skill_ids:
            placeholders = ",".join("?" for _ in skill_ids)
            tool_ids = [
                str(item[0])
                for item in conn.execute(
                    f"select distinct tool_version_id from zr_skill_version_tool where skill_version_id in ({placeholders})",
                    skill_ids,
                )
            ]
        payload = {
            "config_snapshot": snapshot,
            "skill_version_ids": sorted(skill_ids),
            "tool_version_ids": sorted(tool_ids),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        conn.execute(
            "update zr_agent_version set config_snapshot = ?, snapshot_hash = ? where id = ?",
            (json.dumps(snapshot, ensure_ascii=False), digest, version_id),
        )
        changed += 1
    return kb_ids, changed


def _write_builtin_search_env(target: Path) -> dict[str, str]:
    """把平台内置联网搜索配置写入随包分发的默认配置。

    与模型密钥不同，联网搜索是平台内置能力：桌面版首次启动即应可用，
    因此这里把部署者的 .env 配置转成随包资源，由启动器注入 desktop.env。
    """
    defaults = {
        "BUILTIN_MCP_SEARCH_ENABLED": "true",
        "BUILTIN_MCP_SEARCH_ENDPOINT": "http://8.130.167.4:8765/mcp",
        "BUILTIN_MCP_SEARCH_ALLOW_PRIVATE_NETWORK": "true",
        "BUILTIN_MCP_SEARCH_TIMEOUT_SECONDS": "30",
    }
    source_env = ROOT / ".env"
    if source_env.is_file():
        for raw_line in source_env.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("BUILTIN_MCP_SEARCH_") and value.strip():
                defaults[key] = value.strip()
    payload = "".join(f"{key}={value}\n" for key, value in sorted(defaults.items()))
    (target / "builtin-search.env").write_text(payload, encoding="utf-8")
    return defaults


def prepare(source: Path, target: Path) -> Path:
    source_db = source / "z_torque.db"
    if not source_db.is_file():
        raise FileNotFoundError(f"source database not found: {source_db}")

    source_files = source / "files"
    target_db = target / "z_torque.db"
    target.mkdir(parents=True, exist_ok=True)
    if target_db.exists():
        target_db.unlink()
    target_files = target / "files"
    if target_files.exists():
        shutil.rmtree(target_files)

    # Checkpoint first so the packaged database is one self-contained file (no -wal/-shm).
    checkpoint = sqlite3.connect(source_db)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()

    reader = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    writer = sqlite3.connect(target_db)
    try:
        reader.backup(writer)
        writer.execute("PRAGMA journal_mode=DELETE")
        cleared = _scrub(writer)
        paths_normalized = _normalize_knowledge_paths(writer)
        bound_model, bound_versions = _bind_bundled_model(writer)
        kb_ids, kb_versions_bound = _bind_all_knowledge_bases(writer)
        counts = {
            table: writer.execute(f"select count(*) from {table}").fetchone()[0]
            for table in _COUNT_TABLES
            if table in _tables(writer)
        }
        writer.commit()
        writer.execute("VACUUM")
    finally:
        writer.close()
        reader.close()

    copied_files = 0
    if source_files.is_dir():
        shutil.copytree(source_files, target_files)
        copied_files = sum(1 for path in target_files.rglob("*") if path.is_file())

    search_env = _write_builtin_search_env(target)
    digest = hashlib.sha256(target_db.read_bytes()).hexdigest()
    manifest = {
        "purpose": "Z-Torque desktop seed snapshot; deployer credentials removed",
        "business_counts": counts,
        "credentials_cleared": cleared,
        "bound_model": bound_model,
        "agent_versions_bound": bound_versions,
        "knowledge_bases_bound": kb_ids,
        "agent_versions_knowledge_bound": kb_versions_bound,
        "knowledge_paths_normalized": paths_normalized,
        "knowledge_files": copied_files,
        "builtin_search_configured": bool(search_env.get("BUILTIN_MCP_SEARCH_TOKEN")),
        "database_sha256": digest,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Seed snapshot ready: {target}")
    print(f"  agents={counts.get('zr_agent', 0)} skills={counts.get('zr_skill', 0)} "
          f"knowledge_docs={counts.get('zr_knowledge_doc', 0)} files={copied_files}")
    print(f"  cleared credentials: {cleared}")
    return target_db


if __name__ == "__main__":
    prepare(DEFAULT_SOURCE, DEFAULT_TARGET)
