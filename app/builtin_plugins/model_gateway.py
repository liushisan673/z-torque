"""模型网关能力实现（model.provider Port，G2b 迁入）。

OpenAICompatibleGateway / MockModelGateway 属能力实现（能力实现归插件），
本模块按插件→内核方向引用契约（app/kernel/model_gateway/contracts.py：协议与 DTO）。
内核侧 `SkillGateway` / `task/runtime` 等不变量载体仍依赖 contracts 中的
ModelGateway 协议，不感知本模块（装配期经 model.provider Port 注入）。

OpenAICompatibleGateway：OpenAI 兼容 /chat/completions（非流式 + SSE 流式 + 流式聚合 tool_calls），
错误统一映射 ModelGatewayError（超时/限流/鉴权/上游/响应非法）；
MockModelGateway：未配置外部模型时的确定性显式降级（capabilities 可见，勿用于生产）。
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.kernel.model_gateway.contracts import (
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)

logger = logging.getLogger("z_torque.model_gateway")

_UPSTREAM_ERROR = 'model provider request failed'


class OpenAICompatibleGateway(ModelGateway):
    # 思考模型（deepseek-reasoner / deepseek-flash 等）多轮对话要求历史 assistant 消息必须
    # 携带 reasoning_content，否则上游 400："The reasoning_content in the thinking mode must
    # be passed back to the API"。修复前产生的旧历史无 reasoning 存档，按空串占位补齐（上游接受）。
    _THINKING_MARKERS = ("reasoner", "flash")

    def __init__(self, base_url: str, api_key: str, *, default_model: str = "", client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.default_model = default_model
        self.client = client or httpx.AsyncClient()

    @classmethod
    def _is_thinking_model(cls, model: str) -> bool:
        return any(marker in (model or "") for marker in cls._THINKING_MARKERS)

    def _thinking_ready_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload = self._payload(request)
        if self._is_thinking_model(str(payload.get("model") or "")):
            for item in payload["messages"]:
                if item["role"] == "assistant" and not item.get("reasoning_content"):
                    item["reasoning_content"] = ""
        return payload

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload = self._thinking_ready_payload(request)
        payload['stream'] = False
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
        payload = self._thinking_ready_payload(request)
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
        """流式对话的模型决策轮：执行中 yield {'type':'delta'}，结束 yield {'type':'final'}（含工具调用与 usage）。

        产品定义：闲聊不创建 task，由模型判断是否调用工具（tool_calls → 分流创建 task）。
        为保证决策可靠，**带 tools 的请求决策轮与任务链路同构（非流式 generate）**：
        实测上游对「思考模型 + tools + 流式」不返回 tool_calls（finish_reason=stop、content 空/仅计划文本，
        同一请求非流式正常调工具；官方文档思考模式工具调用样例亦为非流式），结果按片段 yield delta
        模拟流式上屏。**无 tools 的纯闲聊不经过决策轮，仍走真流式**（模型不可能发起工具调用）。
        """
        # 入口诊断日志：无论走哪个分支都会打印，用于确认进程加载的代码版本与 tools 数量
        logger.info("stream_response enter: model=%r tools=%d", request.model or self.default_model, len(request.tools or []))
        if request.tools:
            # 决策轮（与任务链路执行循环同构）：显式日志便于控制台确认该分支生效。
            # 只产出 final：文本回复由前端 startTypewriter 播放，服务端不模拟 delta
            logger.info("chat stream decision round with %d tools: non-stream generate (task-chain aligned)", len(request.tools))
            response = await self.generate(request)
            yield {'type': 'final', 'response': response}
            return
        payload = self._thinking_ready_payload(request)
        payload['stream'] = True
        payload['stream_options'] = {'include_usage': True}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
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
                    reasoning = delta.get('reasoning_content')
                    if reasoning:
                        reasoning_parts.append(reasoning)
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
        yield {'type': 'final', 'response': ModelResponse(''.join(content_parts), parsed_calls, finish_reason, provider_id, usage, ''.join(reasoning_parts) or None)}

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
        # 思考模型多轮回传：上游要求历史 assistant 消息必须携带 reasoning_content，否则 400
        if getattr(message, 'reasoning_content', None):
            result['reasoning_content'] = message.reasoning_content
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
        return ModelResponse(message.get('content'), calls, choice.get('finish_reason'), body.get('id'), usage, message.get('reasoning_content'))


class MockModelGateway(ModelGateway):
    """Deterministic provider used when no external model is configured."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        content = request.messages[-1].content if request.messages else ""
        return ModelResponse(content=f"[MOCK] {content}", finish_reason="stop", usage=ModelUsage())

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        response = await self.generate(request)
        yield response.content or ""
