from collections.abc import AsyncIterator

from app.kernel.model_gateway.contracts import ModelGateway, ModelRequest, ModelResponse, ModelUsage


class MockModelGateway(ModelGateway):
    """Deterministic provider used when no external model is configured."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        content = request.messages[-1].content if request.messages else ""
        return ModelResponse(content=f"[MOCK] {content}", finish_reason="stop", usage=ModelUsage())

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        response = await self.generate(request)
        yield response.content or ""
