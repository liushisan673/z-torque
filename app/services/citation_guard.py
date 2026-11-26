"""用户可读知识库引用校验：内部 chunk 标识仅用于系统追踪，不进入回答正文。"""

from __future__ import annotations

import re

from app.kernel.ports.knowledge_provider import KnowledgeChunk

_CITATION_RE = re.compile(r"\[资料:([^\]\n]+)\]")


def validate_citations(answer: str, chunks: list[KnowledgeChunk]) -> dict:
    used = bool(chunks)
    citations = _CITATION_RE.findall(answer or "")
    allowed_names: set[str] = set()
    for chunk in chunks:
        meta = chunk.metadata or {}
        file_name = str(meta.get("file_name") or chunk.source.replace("\\", "/").rsplit("/", 1)[-1])
        allowed_names.add(file_name)
        location: list[str] = []
        if meta.get("page_no") is not None:
            page_label = "张幻灯片" if file_name.lower().endswith(".pptx") else "页"
            location.append(f"第 {meta['page_no']} {page_label}")
        if meta.get("sheet"):
            location.append(f"工作表 {meta['sheet']}")
        if meta.get("row_no") is not None:
            location.append(f"第 {meta['row_no']} 行")
        if meta.get("line_start") is not None:
            end = meta.get("line_end") or meta["line_start"]
            location.append(f"第 {meta['line_start']}-{end} 段")
        if location:
            allowed_names.add(f"{file_name}（{' · '.join(location)}）")
    valid = [name for name in citations if name in allowed_names]
    ok = (not used) or bool(valid)
    return {
        "citation_valid": ok, "used_retrieval": used,
        "citations": citations, "action": "OK" if ok else "REJECTED",
    }
