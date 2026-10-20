"""模型网关契约层：ModelGateway 协议、DTO 与错误类型。

实现类（OpenAICompatibleGateway / MockModelGateway）属能力实现，
位于 app/builtin_plugins/model_gateway.py（G2b 迁入）；内核侧
（SkillGateway / task/runtime / services）只依赖本包契约。
"""

from .contracts import (
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)

__all__ = ['ModelGateway', 'ModelGatewayError', 'ModelMessage', 'ModelRequest', 'ModelResponse', 'ModelToolCall', 'ModelUsage']
