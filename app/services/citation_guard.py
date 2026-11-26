"""用户可读知识库引用校验：内部 chunk 标识仅用于系统追踪，不进入回答正文。"""

from __future__ import annotations

import re

from app.kernel.ports.knowledge_provider import KnowledgeChunk

_CITATION_RE = re.compile(r"\[资料:([^\]\n]+)\]")


def validate_citations(answer: str, chunks: list[KnowledgeChunk]) -> dict:
    used = bool(chunks)
    citations = _CITATION_RE.findall(answer or "")
    allowed_names = {
        str((chunk.metadata or {}).get("file_name") or chunk.source.rsplit("/", 1)[-1])
        for chunk in chunks
    }
    valid = [name for name in citations if name in allowed_names]
    ok = (not used) or bool(valid)
    return {
        "citation_valid": ok, "used_retrieval": used,
        "citations": citations, "action": "OK" if ok else "REJECTED",
    }
