from uuid import uuid4

from app.kernel.ports.knowledge_provider import KnowledgeChunk
from app.kernel.security_context import SecurityContext
from app.services import knowledge_service


class _FakeKnowledgeProvider:
    def __init__(self, document_count=5, message_count=5):
        self.document_count = document_count
        self.message_count = message_count

    def mode(self):
        return "READY"

    def search(self, *, db, tenant_id, query, top_k, filters):
        return [
            KnowledgeChunk(source=f"/docs/{index}.md", content=f"document-{index}", score=-float(index))
            for index in range(min(self.document_count, top_k))
        ]

    def search_messages(self, db, *, tenant_id, user_id, query, top_k, conversation_id=None):
        return [
            KnowledgeChunk(source=f"message:{index}", content=f"message-{index}", score=0.0)
            for index in range(min(self.message_count, top_k))
        ]


def test_knowledge_search_reserves_slots_for_both_sources(monkeypatch):
    monkeypatch.setattr(knowledge_service, "audit_degradation", lambda *_args: None)
    monkeypatch.setattr(knowledge_service, "record_audit_detached", lambda **_kwargs: None)
    monkeypatch.setattr(knowledge_service.get_settings(), "knowledge_message_search_enabled", True)
    ctx = SecurityContext(tenant_id=uuid4(), user_id=uuid4(), trace_id="knowledge-test")

    results = knowledge_service.search(
        object(),
        provider=_FakeKnowledgeProvider(),
        ctx=ctx,
        query="quarterly sales",
        allowed_paths=["/docs"],
        top_k=5,
    )

    assert len(results) == 5
    assert sum(item.source.startswith("message:") for item in results) == 2
    assert sum(item.source.startswith("/docs/") for item in results) == 3


def test_knowledge_search_fills_top_k_from_available_source(monkeypatch):
    monkeypatch.setattr(knowledge_service, "audit_degradation", lambda *_args: None)
    monkeypatch.setattr(knowledge_service, "record_audit_detached", lambda **_kwargs: None)
    monkeypatch.setattr(knowledge_service.get_settings(), "knowledge_message_search_enabled", True)
    ctx = SecurityContext(tenant_id=uuid4(), user_id=uuid4(), trace_id="knowledge-test")

    results = knowledge_service.search(
        object(),
        provider=_FakeKnowledgeProvider(document_count=5, message_count=0),
        ctx=ctx,
        query="quarterly sales",
        allowed_paths=["/docs"],
        top_k=5,
    )

    assert len(results) == 5
    assert all(item.source.startswith("/docs/") for item in results)


def test_answer_citations_hide_internal_chunk_ids():
    chunk_id = str(uuid4())
    chunk = KnowledgeChunk(
        source="/docs/book-policy.pdf",
        content="图书借阅规则",
        metadata={"chunk_id": chunk_id, "file_name": "图书借阅规则.pdf", "page_no": 3},
    )

    answer = knowledge_service.ensure_answer_citations(
        f"借阅期限为三十天。[chunk:{chunk_id}]",
        [chunk],
    )

    assert f"[chunk:{chunk_id}]" not in answer
    assert "图书借阅规则.pdf" in answer
    assert "[资料:图书借阅规则.pdf]" in answer
    assert "第 3 页/幻灯片" in answer

    
def test_stream_filter_hides_internal_references_across_delta_boundaries():
    from app.services.stream_service import _filter_internal_reference_delta

    output = ""
    pending = ""
    parts = ["回答依据", "[chu", "nk:123e4567-e89b-12d3-a456-426614174000]", "。", "[资料:规则.pdf]"]
    for part in parts:
        safe, pending = _filter_internal_reference_delta(pending, part)
        output += safe
    tail, pending = _filter_internal_reference_delta(pending, "", final=True)
    output += tail

    assert "[chunk:" not in output
    assert "[资料:规则.pdf]" in output
    assert output.startswith("回答依据")
