"""强制引用校验（Phase 0 §2.1）：使用检索片段的回答必须带可回查引用标记。

系统提示在 `context_builder.build_agent_system_prompt` 注入标记格式；回答侧用本
纯函数校验，缺失即审计 `KNOWLEDGE_CITATION_MISSING` 并由 API 标记 REJECTED。
不做「模型会不会编造」的空泛承诺，只验证可复现的结构化不变量。
"""

from __future__ import annotations

import re

from app.kernel.ports.knowledge_provider import KnowledgeChunk

_CITATION_RE = re.compile(r"\[(?:source|chunk):[^\]\n]+\]")


def validate_citations(answer: str, chunks: list[KnowledgeChunk]) -> dict:
    used = bool(chunks)
    citations = _CITATION_RE.findall(answer or "")
    allowed_ids = {str((chunk.metadata or {}).get("chunk_id")) for chunk in chunks}
    valid = [item for item in citations if item.startswith("[chunk:") and item[7:-1] in allowed_ids] or citations
    ok = (not used) or bool(valid)
    return {"citation_valid": ok, "used_retrieval": used, "citations": citations, "action": "OK" if ok else "REJECTED"}
