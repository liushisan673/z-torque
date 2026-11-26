from uuid import uuid4

from app.kernel.ports.knowledge_provider import KnowledgeChunk
from app.services.citation_guard import validate_citations


def test_citation_guard_accepts_known_human_readable_sources():
    chunk_id = str(uuid4())
    chunks = [KnowledgeChunk(
        source="manual.pdf", content="source text",
        metadata={"chunk_id": chunk_id, "file_name": "图书借阅规则.pdf"},
    )]
    assert validate_citations("Fact [资料:图书借阅规则.pdf]", chunks)["citation_valid"] is True
    assert validate_citations("Fact [资料:unknown.pdf]", chunks)["citation_valid"] is False
    assert validate_citations(f"Fact [chunk:{chunk_id}]", chunks)["citation_valid"] is False


def test_citation_guard_does_not_require_citation_without_retrieval():
    assert validate_citations("General answer", [])["citation_valid"] is True
