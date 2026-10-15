from collections.abc import AsyncIterator
from typing import Any

from app.kernel.model_gateway import ModelGatewayError


def sse_event(event_id: str, event: str, data: dict[str, Any]) -> str:
    import json
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=True, default=str)}\n\n"


async def stream_text(gateway, request) -> AsyncIterator[str]:
    sequence = 0
    try:
        async for delta in gateway.stream(request):
            sequence += 1
            yield sse_event(str(sequence), "message.delta", {"content": delta})
        sequence += 1
        yield sse_event(str(sequence), "message.done", {"status": "COMPLETED"})
    except ModelGatewayError as exc:
        sequence += 1
        yield sse_event(str(sequence), "message.error", {"code": exc.code, "message": exc.message})
