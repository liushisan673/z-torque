"""模型网关契约层：ModelGateway 协议、DTO 与错误类型。

生产实现类（OpenAICompatibleGateway / ModelRoutingGateway）属能力实现，
位于 app/builtin_plugins/model_gateway.py（G2b 迁入）；测试替身 MockModelGateway
位于 tests/support/fakes.py，仅由测试 DI 注入。
内核侧（SkillGateway / task/runtime / services）只依赖本包契约。
"""

from .contracts import (
    UNKNOWN_TOKEN_VALUE,
    ModelContentPart,
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    normalize_usage,
    parse_model_usage,
)

__all__ = ['ModelContentPart', 'ModelGateway', 'ModelGatewayError', 'ModelMessage', 'ModelRequest', 'ModelResponse', 'ModelToolCall', 'ModelUsage', 'UNKNOWN_TOKEN_VALUE', 'normalize_usage', 'parse_model_usage']
