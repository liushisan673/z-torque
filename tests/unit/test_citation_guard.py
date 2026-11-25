from types import SimpleNamespace

from app.services.citation_guard import validate_citations


def _chunks(*ids):
    return [SimpleNamespace(metadata={"chunk_id": chunk_id}) for chunk_id in ids]


def test_valid_chunk_citation_is_accepted():
    result = validate_citations("销售趋势见 [chunk:chunk-123]", _chunks("chunk-123"))
    assert result["citation_valid"] is True
    assert result["action"] == "OK"


def test_fabricated_chunk_citation_is_rejected():
    result = validate_citations("销售趋势见 [chunk:made-up-id]", _chunks("chunk-123"))
    assert result["citation_valid"] is False
    assert result["action"] == "REJECTED"


def test_source_citation_does_not_replace_chunk_citation():
    result = validate_citations("销售趋势见 [source:report.pdf]", _chunks("chunk-123"))
    assert result["citation_valid"] is False


def test_no_retrieved_chunks_do_not_require_citations():
    result = validate_citations("这是一个通用解释。", [])
    assert result["citation_valid"] is True
