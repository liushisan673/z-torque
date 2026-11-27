from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.prepare_seed_data import (
    _rebuild_knowledge_fts,
    _validate_knowledge_bindings,
    _validate_knowledge_integrity,
)


def _binding_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        create table zr_agent (id text primary key, owner_user_id text);
        create table zr_agent_version (
            id text primary key, tenant_id text, agent_id text, status text, config_snapshot text
        );
        create table zr_knowledge_base (
            id text primary key, tenant_id text, owner_user_id text, status text
        );
        """
    )
    db.execute("insert into zr_agent values ('agent-1', 'user-1')")
    db.execute("insert into zr_knowledge_base values ('kb-1', 'tenant-1', 'user-1', 'ACTIVE')")
    db.execute(
        "insert into zr_agent_version values (?, ?, ?, ?, ?)",
        ("version-1", "tenant-1", "agent-1", "PUBLISHED", json.dumps({"knowledge_base_ids": ["kb-1"]})),
    )
    return db


def test_validate_knowledge_bindings_preserves_role_scoped_ids():
    db = _binding_db()
    before = db.execute("select config_snapshot from zr_agent_version").fetchone()[0]
    ids, bound_versions = _validate_knowledge_bindings(db)
    after = db.execute("select config_snapshot from zr_agent_version").fetchone()[0]

    assert ids == ["kb-1"]
    assert bound_versions == 1
    assert after == before


def test_validate_knowledge_bindings_rejects_foreign_or_missing_kb():
    db = _binding_db()
    db.execute(
        "update zr_agent_version set config_snapshot = ?",
        (json.dumps({"knowledge_base_ids": ["kb-does-not-exist"]}),),
    )
    with pytest.raises(RuntimeError, match="references missing knowledge base"):
        _validate_knowledge_bindings(db)


def test_rebuild_knowledge_fts_recreates_rows_from_chunks():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        create table zr_knowledge_chunk (
            id text primary key, tenant_id text, text text
        );
        create virtual table zr_knowledge_fts using fts5(tenant_id, chunk_id, text, tokenize='unicode61');
        insert into zr_knowledge_chunk values ('chunk-1', 'tenant-1', '欧姆定律 电阻 电流');
        insert into zr_knowledge_fts (tenant_id, chunk_id, text) values ('tenant-1', 'stale', 'stale row');
        """
    )
    count = _rebuild_knowledge_fts(db)
    rows = db.execute("select chunk_id from zr_knowledge_fts").fetchall()

    assert count == 1
    assert rows == [("chunk-1",)]


def test_validate_knowledge_integrity_checks_files_chunks_and_links(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "file-1").write_bytes(b"book")
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        create table zr_file (id text primary key, storage_key text, status text);
        create table zr_knowledge_doc (id text primary key, file_id text, status text, chunk_count integer);
        create table zr_knowledge_chunk (id text primary key, doc_id text, tenant_id text, text text);
        create table zr_knowledge_base_document (file_id text);
        insert into zr_file values ('file-1', 'file-1', 'PARSED');
        insert into zr_knowledge_doc values ('doc-1', 'file-1', 'ACTIVE', 1);
        insert into zr_knowledge_chunk values ('chunk-1', 'doc-1', 'tenant-1', '知识内容');
        insert into zr_knowledge_base_document values ('file-1');
        """
    )

    result = _validate_knowledge_integrity(db, files)
    assert result == {
        "knowledge_documents": 1,
        "knowledge_chunks": 1,
        "knowledge_files": 1,
        "knowledge_base_file_links": 1,
    }


def test_validate_knowledge_integrity_rejects_chunk_count_mismatch(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "file-1").write_bytes(b"book")
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        create table zr_file (id text primary key, storage_key text, status text);
        create table zr_knowledge_doc (id text primary key, file_id text, status text, chunk_count integer);
        create table zr_knowledge_chunk (id text primary key, doc_id text, tenant_id text, text text);
        create table zr_knowledge_base_document (file_id text);
        insert into zr_file values ('file-1', 'file-1', 'PARSED');
        insert into zr_knowledge_doc values ('doc-1', 'file-1', 'ACTIVE', 2);
        insert into zr_knowledge_chunk values ('chunk-1', 'doc-1', 'tenant-1', '知识内容');
        insert into zr_knowledge_base_document values ('file-1');
        """
    )

    with pytest.raises(RuntimeError, match="knowledge chunk count mismatch"):
        _validate_knowledge_integrity(db, files)
