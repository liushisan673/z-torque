from uuid import uuid4

from app.kernel.ports.knowledge_provider import KnowledgeChunk
from app.services.citation_guard import validate_citations


def test_citation_guard_accepts_only_known_chunk_ids():
    allowed = str(uuid4())
    other = str(uuid4())
    chunks = [KnowledgeChunk(source="manual.pdf", content="source text", metadata={"chunk_id": allowed})]
    assert validate_citations(f"Fact [chunk:{allowed}]", chunks)["citation_valid"] is True
    assert validate_citations(f"Fact [chunk:{other}]", chunks)["citation_valid"] is False
    assert validate_citations("Fact [source:manual.pdf]", chunks)["citation_valid"] is False


def test_citation_guard_does_not_require_citation_without_retrieval():
    assert validate_citations("General answer", [])["citation_valid"] is True
