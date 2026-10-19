from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import UUID


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


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


@dataclass(frozen=True)
class ModelResponse:
    content: str | None = None
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    usage: ModelUsage | None = None


@dataclass(frozen=True)
class ModelRequest:
    trace_id: str
    conversation_id: UUID
    messages: list[ModelMessage]
    model: str
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
