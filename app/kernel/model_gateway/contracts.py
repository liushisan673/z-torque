from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal
from uuid import UUID


@dataclass(frozen=True)
class ModelContentPart:
    """多模态消息片段；``image_url`` 为 data URI 或可访问 URL。"""

    type: Literal["text", "image_url"]
    text: str | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str | None = None
    # 为空时行为与纯文本链路完全一致（网关只输出 {role, content}）
    content_parts: list[ModelContentPart] | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    # 思考模型（如 deepseek-flash）的思考过程：多轮回传时官方要求必须携带，否则上游 400
    reasoning_content: str | None = None


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # cache 字段与「是否由上游真实返回」标记：缺省一律 unknown，禁止把缺失伪装成 0。
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    prompt_tokens_known: bool = False
    completion_tokens_known: bool = False
    total_tokens_known: bool = False
    cache_read_tokens_known: bool = False
    cache_write_tokens_known: bool = False


UNKNOWN_TOKEN_VALUE = "unknown"


def parse_model_usage(data: Any) -> ModelUsage:
    """把上游 usage 对象解析为带「未知」语义的模型契约。

    只有键真实出现且为非负整数时才标记 known；缺失、None、负数一律 unknown。
    """

    def _field(key: str) -> tuple[int, bool]:
        if not isinstance(data, dict) or key not in data:
            return 0, False
        known_key = f"{key}_known"
        if known_key in data and data[known_key] is False:
            return 0, False
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0, False
        return value, True

    prompt, prompt_known = _field("prompt_tokens")
    completion, completion_known = _field("completion_tokens")
    total, total_known = _field("total_tokens")
    cache_read, cache_read_known = _field("cache_read_tokens")
    cache_write, cache_write_known = _field("cache_write_tokens")
    if not total_known and prompt_known and completion_known:
        total, total_known = prompt + completion, True
    return ModelUsage(
        prompt, completion, total, cache_read, cache_write,
        prompt_known, completion_known, total_known, cache_read_known, cache_write_known,
    )


def normalize_usage(usage: ModelUsage | None) -> dict[str, int | str]:
    """审计/汇总统一出口：未提供写 `unknown`，不写 0。"""
    source = usage or ModelUsage()
    return {
        "prompt_tokens": source.prompt_tokens if source.prompt_tokens_known else UNKNOWN_TOKEN_VALUE,
        "completion_tokens": source.completion_tokens if source.completion_tokens_known else UNKNOWN_TOKEN_VALUE,
        "total_tokens": source.total_tokens if source.total_tokens_known else UNKNOWN_TOKEN_VALUE,
        "cache_read_tokens": source.cache_read_tokens if source.cache_read_tokens_known else UNKNOWN_TOKEN_VALUE,
        "cache_write_tokens": source.cache_write_tokens if source.cache_write_tokens_known else UNKNOWN_TOKEN_VALUE,
    }


@dataclass(frozen=True)
class ModelResponse:
    content: str | None = None
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    usage: ModelUsage | None = None
    reasoning_content: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    trace_id: str
    messages: list[ModelMessage]
    model: str
    # 模型路由所需：按租户查 ModelResource → Provider → Endpoint；为空时回退全局网关
    tenant_id: UUID | None = None
    # 无会话任务（POST /tasks）没有 conversation_id：上下文改由 task.request_summary 提供
    conversation_id: UUID | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: str | dict[str, Any] = "auto"
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float = 60.0
    stream: bool = False
    task_id: UUID | None = None
    agent_version_id: UUID | None = None
    round_number: int = 0


class ModelGatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ModelGateway:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        raise NotImplementedError

    async def stream_response(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
        """默认实现：包装纯文本 stream() 聚合全文，final 不含 tool_calls（真实网关应覆写以支持流式分流）。"""
        parts: list[str] = []
        async for delta in self.stream(request):
            parts.append(delta)
            yield {'type': 'delta', 'content': delta}
        yield {'type': 'final', 'response': ModelResponse(''.join(parts), [], 'stop', None, ModelUsage())}
