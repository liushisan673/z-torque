"""上下文治理原语（路线图 v2.0 §2.4）。

设计约束：
- 不引入 tokenizer 依赖（单机 exe 打包体积）：字符数 / 可配置系数做 token 估算。
- 自愈管线顺序写死：strip_placeholder → strip_malformed_tool_calls →
  drop_orphan_tool_results → backfill_missing_tool_results → apply_tool_result_budget。
- 硬不变量：工具调用与 toolResult 成对保留；任何裁剪不得单独丢弃其中一方。
- 纯函数、无 DB 副作用，供 build_context 与 ExecutionLoop 两处复用。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.kernel.model_gateway import ModelMessage

# 系统内部注入的占位内容：构建 prompt 前结构性剔除（不靠提示词警告）。
PLACEHOLDER_MARKERS = ("[MOCK]", "[PLACEHOLDER]")
# 回填的缺失工具结果：不属于系统占位符，下一轮 strip_placeholder 不剔除它，
# 保证「成对」不变量在跨轮次间稳定成立。
BACKFILL_TOOL_RESULT = "[tool result unavailable]"

# §2.4-8 来源分层：不可信内容中的聊天模板注入标记在构建 prompt 前结构性剔除，
# 不靠「untrusted」文字警告。展示层按该 suffix 精确剥离内部续跑提示。
UNTRUSTED_TEMPLATE_TOKENS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "[INST]", "[/INST]", "<<SYS>>", "<</SYS>>")
CONTINUATION_MARKER_SUFFIX = "\n<!-- zt-internal-continuation -->"

# 自愈管线第 5 步默认上限（与 ExecutionLoop 既有 20000 字符口径对齐）。
DEFAULT_TOOL_RESULT_MAX_CHARS = 20000


def sanitize_untrusted(text: str | None) -> str | None:
    """结构性剔除不可信片段里的聊天模板控制 token（不做语义改写）。"""
    if not text:
        return text
    cleaned = text
    for token in UNTRUSTED_TEMPLATE_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned


def estimate_tokens(text: str | None, chars_per_token: float) -> int:
    """字符数 / 系数估算 token；空串返回 0，最少 1。"""
    if not text:
        return 0
    return max(1, int(len(text) / max(0.1, chars_per_token)))


def message_tokens(item: ModelMessage, chars_per_token: float) -> int:
    """单条 ModelMessage 的估算 token（含 content_parts 文本与 tool_calls 参数）。"""
    total = estimate_tokens(item.content, chars_per_token)
    for part in item.content_parts or []:
        if getattr(part, "type", None) == "text" and getattr(part, "text", None):
            total += estimate_tokens(part.text, chars_per_token)
    if item.tool_calls:
        total += estimate_tokens(json.dumps(item.tool_calls, ensure_ascii=False, default=str), chars_per_token)
    return total


def context_tokens(messages: list[ModelMessage], chars_per_token: float) -> int:
    return sum(message_tokens(item, chars_per_token) for item in messages)


def _tool_call_id(call: Any) -> str | None:
    if not isinstance(call, dict):
        return None
    value = call.get("id")
    return str(value) if value else None


def strip_placeholder(messages: list[ModelMessage]) -> list[ModelMessage]:
    """第 1 步：剔除完全由系统占位标记构成的短消息。"""
    kept: list[ModelMessage] = []
    for item in messages:
        content = (item.content or "").strip()
        if content and len(content) <= 64 and any(marker in content for marker in PLACEHOLDER_MARKERS):
            continue
        kept.append(item)
    return kept


def strip_malformed_tool_calls(messages: list[ModelMessage]) -> list[ModelMessage]:
    """第 2 步：剥离畸形 tool_calls；整轮无有效 call 时降级为纯文本 assistant。"""
    kept: list[ModelMessage] = []
    for item in messages:
        if item.role == "assistant" and item.tool_calls:
            valid: list[dict[str, Any]] = []
            for call in item.tool_calls:
                if not isinstance(call, dict):
                    continue
                if not _tool_call_id(call) or not call.get("name"):
                    continue
                arguments = call.get("arguments")
                if isinstance(arguments, str):
                    try:
                        json.loads(arguments)
                    except (TypeError, ValueError):
                        continue
                elif arguments is not None and not isinstance(arguments, dict):
                    continue
                valid.append(call)
            if valid:
                kept.append(ModelMessage(
                    item.role, item.content, content_parts=item.content_parts,
                    tool_calls=valid, reasoning_content=item.reasoning_content,
                ))
            elif item.content:
                kept.append(ModelMessage(
                    item.role, item.content, content_parts=item.content_parts,
                    reasoning_content=item.reasoning_content,
                ))
            # 否则整条丢弃（既无有效 tool call 也无文本）
            continue
        kept.append(item)
    return kept


def drop_orphan_tool_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    """第 3 步：丢弃没有对应 assistant tool_calls 的 tool 消息。"""
    known_ids: set[str] = set()
    for item in messages:
        if item.role == "assistant":
            for call in item.tool_calls or []:
                call_id = _tool_call_id(call)
                if call_id:
                    known_ids.add(call_id)
    return [
        item for item in messages
        if not (item.role == "tool" and (not item.tool_call_id or item.tool_call_id not in known_ids))
    ]


def backfill_missing_tool_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    """第 4 步：对每个未收到结果的 tool_call 回填占位 tool 消息，恢复成对。"""
    answered = {
        item.tool_call_id for item in messages
        if item.role == "tool" and item.tool_call_id
    }
    healed: list[ModelMessage] = []
    for item in messages:
        healed.append(item)
        if item.role == "assistant":
            for call in item.tool_calls or []:
                call_id = _tool_call_id(call)
                if call_id and call_id not in answered:
                    healed.append(ModelMessage("tool", BACKFILL_TOOL_RESULT, tool_call_id=call_id))
                    answered.add(call_id)
    return healed


def apply_tool_result_budget(messages: list[ModelMessage], max_chars: int) -> list[ModelMessage]:
    """第 5 步：超长 toolResult 截断（保留消息与 id，不破坏成对）。"""
    kept: list[ModelMessage] = []
    for item in messages:
        if item.role == "tool" and item.content and len(item.content) > max_chars:
            kept.append(ModelMessage(
                item.role, item.content[:max_chars] + "\n…[truncated]",
                content_parts=item.content_parts, tool_call_id=item.tool_call_id,
                reasoning_content=item.reasoning_content,
            ))
        else:
            kept.append(item)
    return kept


def heal_context(messages: list[ModelMessage], *, tool_result_max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS) -> list[ModelMessage]:
    """按写死顺序运行自愈管线；返回新列表，不修改入参。"""
    result = list(messages)
    result = strip_placeholder(result)
    result = strip_malformed_tool_calls(result)
    result = drop_orphan_tool_results(result)
    result = backfill_missing_tool_results(result)
    result = apply_tool_result_budget(result, tool_result_max_chars)
    return result


def tool_pairs_balanced(messages: list[ModelMessage]) -> bool:
    """不变量校验：每个 tool_call_id 必须同时存在 assistant 声明与 tool 结果。"""
    declared: set[str] = set()
    answered: set[str] = set()
    for item in messages:
        if item.role == "assistant":
            for call in item.tool_calls or []:
                call_id = _tool_call_id(call)
                if call_id:
                    declared.add(call_id)
        elif item.role == "tool" and item.tool_call_id:
            answered.add(item.tool_call_id)
    return declared == answered


def stable_prefix(messages: list[ModelMessage]) -> list[ModelMessage]:
    """稳定前缀：system 消息去重前置且保持原顺序，其余原序；不引入 per-turn 时间戳。"""
    systems: list[ModelMessage] = []
    rest: list[ModelMessage] = []
    seen: set[str] = set()
    for item in messages:
        if item.role == "system":
            key = item.content or ""
            if key in seen:
                continue
            seen.add(key)
            systems.append(item)
        else:
            rest.append(item)
    return systems + rest


def history_hash(messages: list[ModelMessage]) -> str:
    """accepted-history 哈希：只含语义字段，排除 per-turn 元数据。"""
    payload = [
        {
            "role": item.role,
            "content": item.content,
            "tool_call_id": item.tool_call_id,
            "tool_calls": item.tool_calls,
            "reasoning_content": item.reasoning_content,
        }
        for item in messages
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def split_accepted_delta(messages: list[ModelMessage]) -> tuple[list[ModelMessage], list[ModelMessage]]:
    """H/delta 分离：最后一个 USER 消息（含）之前为已接受前缀 H，之后为增量 delta。

    只有一个 user 或无 user 时前缀为空——当前轮增量不得摘要，避免把未发送内容写进 summary。
    """
    index = None
    for position in range(len(messages) - 1, -1, -1):
        if messages[position].role == "user":
            index = position
            break
    if index is None or index == 0:
        return [], list(messages)
    return list(messages[:index]), list(messages[index:])


def extractive_summary(messages: list[ModelMessage], *, max_lines: int = 24, line_chars: int = 160) -> str:
    """确定性抽取式摘要：不依赖 LLM，作为默认压缩器；失败由调用方回退保留原历史。"""
    lines: list[str] = []
    for item in messages:
        if item.role == "system":
            continue
        text = (item.content or "").strip().replace("\n", " ")
        if not text:
            continue
        label = "用户" if item.role == "user" else "助手" if item.role == "assistant" else "工具"
        lines.append(f"{label}: {text[:line_chars]}")
    if not lines:
        raise ValueError("no compressible history")
    return "\n".join(lines[-max_lines:])
