"""Phase 0 §2.1 真实入口验收：FTS5 + 坐标 chunk + 强制引用 + 删除级联。

全程走 HTTP API（上传 → 检索 → 引用校验 → 删除）与 capabilities，不 mock 检索逻辑。
golden set 在 tests/fixtures/knowledge_golden.json，验证命中与引用坐标准确率。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AuditLog,
    AuthSession,
    Conversation,
    File,
    KnowledgeChunk,
    KnowledgeDoc,
    Message,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from app.services.knowledge_service import _DEGRADED_AUDITED

_GOLDEN = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "knowledge_golden.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        # FTS 表由应用侧双写（无 SQL 触发器），测试清理需显式清空，避免跨用例污染
        db.execute(text("DELETE FROM zr_knowledge_fts"))
        for model in (Message, KnowledgeChunk, KnowledgeDoc, File, AuditLog, AuthSession, Conversation, Agent, User, Tenant):
            db.execute(delete(model))
    _DEGRADED_AUDITED.clear()


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"knowledge-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        return tenant.id, user.id


def _username(user_id: UUID) -> str:
    with SessionLocal() as db:
        return db.get(User, user_id).username


async def _login(client: AsyncClient, user_id: UUID) -> dict:
    response = await client.post("/api/z-torque/auth/login", json={"username": _username(user_id), "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _make_pdf(text: str) -> bytes:
    """构造最小可解析 PDF（pypdf 可 extract_text），避免引入新的测试依赖。"""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 16 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(out)


@pytest.mark.asyncio
async def test_capabilities_reports_local_search_state_and_citation_enforcement():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200
    knowledge = response.json()["data"]["knowledge"]
    assert knowledge["local"]["search"] in {"AVAILABLE", "DEGRADED"}
    assert knowledge["citation_enforcement"] is True


@pytest.mark.asyncio
async def test_golden_set_pdf_upload_search_returns_chunk_with_page_coordinate():
    """golden set：上传 PDF → 检索 → source+page，坐标能回查 chunk 表。"""
    _, user_id = _bootstrap()
    case = _GOLDEN[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, user_id)
        uploaded = await client.post(
            "/api/z-torque/files", headers=headers,
            files={"upload": (case["document"], _make_pdf(case["expected_text"]), "application/pdf")},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["data"]["status"] == "PARSED", uploaded.text
        file_id = UUID(uploaded.json()["data"]["id"])
        searched = await client.post(
            "/api/z-torque/knowledge/search", headers=headers,
            json={"query": case["query"], "top_k": 5},
        )
    assert searched.status_code == 200, searched.text
    chunks = searched.json()["data"]["chunks"]
    assert chunks, "golden query should hit at least one chunk"
    hit = next(item for item in chunks if case["expected_text"] in item["content"])
    assert hit["source"]
    assert hit["page_no"] == case["expected_coord"]["page_no"]
    with SessionLocal() as db:
        row = db.get(KnowledgeChunk, UUID(hit["chunk_id"]))
        assert row is not None and row.page_no == 1
        assert db.scalar(select(KnowledgeDoc).where(KnowledgeDoc.file_id == file_id)) is not None


@pytest.mark.asyncio
async def test_delete_file_removes_search_hits():
    _, user_id = _bootstrap()
    case = _GOLDEN[1]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, user_id)
        uploaded = await client.post(
            "/api/z-torque/files", headers=headers,
            files={"upload": (case["document"], _make_pdf(case["expected_text"]), "application/pdf")},
        )
        file_id = UUID(uploaded.json()["data"]["id"])
        before = await client.post("/api/z-torque/knowledge/search", headers=headers, json={"query": case["query"]})
        assert any(case["expected_text"] in item["content"] for item in before.json()["data"]["chunks"])
        removed = await client.delete(f"/api/z-torque/files/{file_id}", headers=headers)
        assert removed.status_code == 200, removed.text
        after = await client.post("/api/z-torque/knowledge/search", headers=headers, json={"query": case["query"]})
    assert after.json()["data"]["chunks"] == []
    with SessionLocal() as db:
        assert db.scalar(select(KnowledgeDoc).where(KnowledgeDoc.file_id == file_id)) is None


@pytest.mark.asyncio
async def test_answer_without_citation_is_rejected_and_audited():
    _, user_id = _bootstrap()
    case = _GOLDEN[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, user_id)
        await client.post(
            "/api/z-torque/files", headers=headers,
            files={"upload": (case["document"], _make_pdf(case["expected_text"]), "application/pdf")},
        )
        found = await client.post("/api/z-torque/knowledge/search", headers=headers, json={"query": case["query"]})
        chunk_id = found.json()["data"]["chunks"][0]["chunk_id"]
        missing = await client.post("/api/z-torque/knowledge/verify-answer", headers=headers, json={"answer": "这是没有依据的回答", "chunk_ids": [chunk_id]})
        cited = await client.post("/api/z-torque/knowledge/verify-answer", headers=headers, json={"answer": f"依据片段 [chunk:{chunk_id}]", "chunk_ids": [chunk_id]})
    assert missing.json()["data"]["action"] == "REJECTED"
    assert missing.json()["data"]["citation_valid"] is False
    assert cited.json()["data"]["action"] == "OK"
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "KNOWLEDGE_CITATION_MISSING")) is not None


@pytest.mark.asyncio
async def test_context_does_not_inject_full_extracted_text():
    tenant_id, user_id = _bootstrap()
    text = "\n".join(f"line-{index}: " + "filler " * 40 for index in range(1, 121))
    with SessionLocal.begin() as db:
        agent = Agent(tenant_id=tenant_id, owner_user_id=None, name="ctx-agent", source_type="OFFICIAL", status="ACTIVE")
        db.add(agent)
        db.flush()
        conversation = Conversation(tenant_id=tenant_id, owner_user_id=user_id, agent_id=agent.id, title="ctx")
        db.add(conversation)
        db.flush()
        conversation_id = conversation.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, user_id)
        uploaded = await client.post(
            "/api/z-torque/files", headers=headers,
            params={"conversation_id": str(conversation_id)},
            files={"upload": ("context.md", text.encode("utf-8"), "text/markdown")},
        )
        assert uploaded.status_code == 200, uploaded.text
        file_id = UUID(uploaded.json()["data"]["id"])
    with SessionLocal.begin() as db:
        message = Message(
            tenant_id=tenant_id, conversation_id=conversation_id, sender_type="USER",
            content="line-100", content_parts=[{"type": "file", "file_id": str(file_id)}], status="COMPLETED",
        )
        db.add(message)
        db.flush()
    from app.services.context_builder import build_context

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        context = build_context(db, conversation)
    rendered = "\n".join(item.content or "" for item in context)
    assert "[chunk:" in rendered
    assert len(rendered) < len(text) // 2, "整段 extracted_text 不应再进入 prompt"


def test_cjk_trigram_retrieval_and_global_user_knowledge_context():
    """中文无空格检索可命中，且 Agent 会话能检索用户未作为附件发送的已上传文档。"""
    tenant_id, user_a = _bootstrap()
    with SessionLocal.begin() as db:
        user_b = User(tenant_id=tenant_id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user_b)
        db.flush()
        user_b_id = user_b.id

    source_text = "Z-Torque 本地知识库使用 SQLite FTS5 trigram 检索，中文短语无需空格也可以召回相关文档。"
    async def upload_document():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = await _login(client, user_a)
            response = await client.post(
                "/api/z-torque/files", headers=headers,
                files={"upload": ("sqlite-knowledge.md", source_text.encode("utf-8"), "text/markdown")},
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "PARSED", response.text

    import asyncio
    asyncio.run(upload_document())

    conversation_ids = {}
    with SessionLocal.begin() as db:
        agent = Agent(tenant_id=tenant_id, owner_user_id=None, name="knowledge-supervisor", source_type="OFFICIAL", status="ACTIVE")
        db.add(agent)
        db.flush()
        for user_id in (user_a, user_b_id):
            conversation = Conversation(tenant_id=tenant_id, owner_user_id=user_id, agent_id=agent.id, title="knowledge retrieval")
            db.add(conversation)
            db.flush()
            db.add(Message(
                tenant_id=tenant_id, conversation_id=conversation.id, sender_type="USER",
                content="请解释 SQLite 知识库的中文检索方式", status="COMPLETED",
            ))
            conversation_ids[user_id] = conversation.id

    from app.services.context_builder import build_context

    with SessionLocal() as db:
        conversation_a = db.get(Conversation, conversation_ids[user_a])
        context_a = build_context(db, conversation_a)
        conversation_b = db.get(Conversation, conversation_ids[user_b_id])
        context_b = build_context(db, conversation_b)

    rendered_a = "\\n".join(item.content or "" for item in context_a)
    rendered_b = "\\n".join(item.content or "" for item in context_b)
    assert "SQLite FTS5 trigram 检索" in rendered_a
    assert "[chunk:" in rendered_a
    assert "SQLite FTS5 trigram 检索" not in rendered_b


@pytest.mark.asyncio
async def test_degraded_state_is_visible_and_audited(monkeypatch):
    _, user_id = _bootstrap()
    registry = app.state.port_registry
    provider = registry.try_get("knowledge.provider")
    assert provider is not None
    monkeypatch.setattr(provider, "mode", lambda: "DEGRADED")
    monkeypatch.setattr(provider, "last_degradation_reason", "simulated FTS5 unavailable")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, user_id)
        status = await client.get("/api/z-torque/knowledge/status", headers=headers)
        capabilities = await client.get("/api/z-torque/capabilities")
    assert status.json()["data"]["search"] == "DEGRADED"
    assert capabilities.json()["data"]["knowledge"]["local"]["search"] == "DEGRADED"
    with SessionLocal() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "KNOWLEDGE_DEGRADED"))
        assert audit is not None and audit.detail["state"] == "DEGRADED"


@pytest.mark.asyncio
async def test_message_level_search_is_enabled_and_scoped_to_owner():
    """消息级检索已全量开启：按登录用户隔离会话，capabilities 上报 + metadata-only 审计。"""
    tenant_a, user_a = _bootstrap()
    tenant_b, user_b = _bootstrap()
    with SessionLocal.begin() as db:
        for tenant_id, user_id, token in (
            (tenant_a, user_a, "zqmsgalpha-owner-a"),
            (tenant_b, user_b, "zqmsgbeta-owner-b"),
        ):
            agent = Agent(tenant_id=tenant_id, owner_user_id=None, name=f"msg-{uuid4()}", source_type="OFFICIAL", status="ACTIVE")
            db.add(agent)
            db.flush()
            conversation = Conversation(tenant_id=tenant_id, owner_user_id=user_id, agent_id=agent.id, title="msg")
            db.add(conversation)
            db.flush()
            db.add(Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="USER", content=token, status="COMPLETED"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers_a = await _login(client, user_a)
        hit = await client.post("/api/z-torque/knowledge/search", headers=headers_a, json={"query": "zqmsgalpha", "top_k": 5})
        cross = await client.post("/api/z-torque/knowledge/search", headers=headers_a, json={"query": "zqmsgbeta", "top_k": 5})
        capabilities = await client.get("/api/z-torque/capabilities")
    assert hit.status_code == 200, hit.text
    messages = [item for item in hit.json()["data"]["chunks"] if item.get("source_type") == "message"]
    assert messages and "zqmsgalpha-owner-a" in messages[0]["content"]
    assert all("zqmsgbeta" not in item["content"] for item in cross.json()["data"]["chunks"])
    assert capabilities.json()["data"]["knowledge"]["message_search"] == "AVAILABLE"
    with SessionLocal() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "KNOWLEDGE_MESSAGE_SEARCH"))
        assert audit is not None
        assert "query" not in audit.detail and len(audit.detail["query_hash"]) == 64
