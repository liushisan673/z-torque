from collections.abc import AsyncIterator
from typing import Any
import json
import httpx
from app.kernel.model_gateway.contracts import ModelGateway, ModelGatewayError, ModelMessage, ModelRequest, ModelResponse, ModelToolCall, ModelUsage

from app.kernel.model_gateway.contracts import ModelGateway

_UPSTREAM_ERROR = 'model provider request failed'

class OpenAICompatibleGateway(ModelGateway):
    def __init__(self, base_url: str, api_key: str, *, default_model: str = "", client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.default_model = default_model
        self.client = client or httpx.AsyncClient()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {'model': request.model or self.default_model, 'messages': [self._message(m) for m in request.messages], 'stream': False}
        if request.tools:
            payload['tools'] = request.tools
            payload['tool_choice'] = request.tool_choice
        if request.temperature is not None:
            payload['temperature'] = request.temperature
        if request.max_tokens is not None:
            payload['max_tokens'] = request.max_tokens
        try:
            response = await self.client.post(f'{self.base_url}/chat/completions', headers={'Authorization': f'Bearer {self.api_key}'}, json=payload, timeout=request.timeout_seconds)
            response.raise_for_status()
            return self._normalize(response.json())
        except httpx.TimeoutException as exc:
            raise ModelGatewayError('AI_MODEL_TIMEOUT', 'model request timed out') from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = 'AI_MODEL_RATE_LIMITED' if status == 429 else ('AI_MODEL_AUTH_ERROR' if status in (401, 403) else 'AI_MODEL_UPSTREAM_ERROR')
            raise ModelGatewayError(code, _UPSTREAM_ERROR) from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ModelGatewayError('AI_MODEL_RESPONSE_INVALID', 'model response is invalid') from exc

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        payload = self._payload(request)
        payload['stream'] = True
        try:
            async with self.client.stream('POST', f'{self.base_url}/chat/completions', headers={'Authorization': f'Bearer {self.api_key}', 'Accept': 'text/event-stream'}, json=payload, timeout=request.timeout_seconds) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        return
                    try:
                        delta = (json.loads(data).get('choices') or [{}])[0].get('delta') or {}
                        content = delta.get('content')
                        if content:
                            yield content
                    except (ValueError, TypeError, KeyError) as exc:
                        raise ModelGatewayError('AI_MODEL_RESPONSE_INVALID', 'model stream response is invalid') from exc
        except httpx.TimeoutException as exc:
            raise ModelGatewayError('AI_MODEL_TIMEOUT', 'model request timed out') from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = 'AI_MODEL_RATE_LIMITED' if status == 429 else ('AI_MODEL_AUTH_ERROR' if status in (401, 403) else 'AI_MODEL_UPSTREAM_ERROR')
            raise ModelGatewayError(code, _UPSTREAM_ERROR) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError('AI_MODEL_UPSTREAM_ERROR', _UPSTREAM_ERROR) from exc

    async def stream_response(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
        """流式调用并聚合 tool_calls：执行中 yield {'type':'delta'}，结束 yield {'type':'final'}（含工具调用与 usage）。"""
        payload = self._payload(request)
        payload['stream'] = True
        payload['stream_options'] = {'include_usage': True}
        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        provider_id: str | None = None
        usage = ModelUsage()
        try:
            async with self.client.stream('POST', f'{self.base_url}/chat/completions', headers={'Authorization': f'Bearer {self.api_key}', 'Accept': 'text/event-stream'}, json=payload, timeout=request.timeout_seconds) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        body = json.loads(data)
                    except ValueError as exc:
                        raise ModelGatewayError('AI_MODEL_RESPONSE_INVALID', 'model stream response is invalid') from exc
                    if body.get('id'):
                        provider_id = str(body['id'])
                    if body.get('usage'):
                        usage_data = body['usage']
                        usage = ModelUsage(usage_data.get('prompt_tokens', 0), usage_data.get('completion_tokens', 0), usage_data.get('total_tokens', 0))
                    choices = body.get('choices') or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get('finish_reason'):
                        finish_reason = str(choice['finish_reason'])
                    delta = choice.get('delta') or {}
                    content = delta.get('content')
                    if content:
                        content_parts.append(content)
                        yield {'type': 'delta', 'content': content}
                    for tc in delta.get('tool_calls') or []:
                        index = int(tc.get('index') or 0)
                        slot = calls.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
                        if tc.get('id'):
                            slot['id'] = str(tc['id'])
                        function = tc.get('function') or {}
                        if function.get('name'):
                            slot['name'] = str(function['name'])
                        if function.get('arguments'):
                            slot['arguments'] += str(function['arguments'])
        except httpx.TimeoutException as exc:
            raise ModelGatewayError('AI_MODEL_TIMEOUT', 'model request timed out') from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            code = 'AI_MODEL_RATE_LIMITED' if status == 429 else ('AI_MODEL_AUTH_ERROR' if status in (401, 403) else 'AI_MODEL_UPSTREAM_ERROR')
            raise ModelGatewayError(code, _UPSTREAM_ERROR) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError('AI_MODEL_UPSTREAM_ERROR', _UPSTREAM_ERROR) from exc
        parsed_calls: list[ModelToolCall] = []
        for index in sorted(calls):
            slot = calls[index]
            if not slot['name']:
                continue
            try:
                arguments = json.loads(slot['arguments']) if slot['arguments'] else {}
            except ValueError as exc:
                raise ModelGatewayError('AI_MODEL_RESPONSE_INVALID', 'model stream response is invalid') from exc
            if not isinstance(arguments, dict):
                raise ModelGatewayError('AI_MODEL_RESPONSE_INVALID', 'model stream response is invalid')
            parsed_calls.append(ModelToolCall(slot['id'] or f'call_{index}', slot['name'], arguments))
        yield {'type': 'final', 'response': ModelResponse(''.join(content_parts), parsed_calls, finish_reason, provider_id, usage)}

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {'model': request.model or self.default_model, 'messages': [self._message(m) for m in request.messages], 'stream': request.stream}
        if request.tools:
            payload['tools'] = request.tools
            payload['tool_choice'] = request.tool_choice
        if request.temperature is not None:
            payload['temperature'] = request.temperature
        if request.max_tokens is not None:
            payload['max_tokens'] = request.max_tokens
        return payload

    @staticmethod
    def _message(message: ModelMessage) -> dict[str, Any]:
        result: dict[str, Any] = {'role': message.role, 'content': message.content}
        if message.tool_call_id:
            result['tool_call_id'] = message.tool_call_id
        if message.tool_calls:
            result['tool_calls'] = [OpenAICompatibleGateway._tool_call(call) for call in message.tool_calls]
        return result

    @staticmethod
    def _tool_call(call: Any) -> dict[str, Any]:
        if isinstance(call, dict):
            call_id = call.get('id')
            name = call.get('name')
            arguments = call.get('arguments')
        else:
            call_id = call.id
            name = call.name
            arguments = call.arguments
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}

    @staticmethod
    def _normalize(body: dict[str, Any]) -> ModelResponse:
        choice = body['choices'][0]
        message = choice['message']
        calls = []
        for call in message.get('tool_calls') or []:
            function = call.get('function', {})
            arguments = function.get('arguments', {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError('tool arguments must be an object')
            calls.append(ModelToolCall(str(call['id']), str(function['name']), arguments))
        usage_data = body.get('usage') or {}
        usage = ModelUsage(usage_data.get('prompt_tokens', 0), usage_data.get('completion_tokens', 0), usage_data.get('total_tokens', 0))
        return ModelResponse(message.get('content'), calls, choice.get('finish_reason'), body.get('id'), usage)
