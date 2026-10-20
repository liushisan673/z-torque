"""model.selector Port 的规则选择器实现（rule-based-model-selector，文档 §6.1）。

规则（按优先级）：
1. AgentVersion 快照绑定直通（快照语义：发布后不随全局配置漂移）；
2. 白名单校验（仅在显式配置 model_allowed_names 时生效，拦截快照中的任意模型名，
   未命中回退默认并标记 whitelist_fallback）；
3. 默认回退（settings.model_name）。

调用参数（temperature / max_tokens）与模型同名同规则：快照值优先（逐键），
白名单回退时参数一并回退全局默认（settings.model_temperature / model_max_tokens）。
"""

from app.builtin_plugins.manifest import AssemblyContext, PluginManifest
from app.kernel.ports.model_selector import (
    SELECTION_SOURCE_DEFAULT,
    SELECTION_SOURCE_SNAPSHOT,
    SELECTION_SOURCE_WHITELIST_FALLBACK,
    ModelSelection,
    ModelSelectionRequest,
    ModelSelector,
)
from app.kernel.registry.port_registry import PortPriority


def _or_default(snapshot_value, default_value):
    return snapshot_value if snapshot_value is not None else default_value


class RuleBasedModelSelector(ModelSelector):
    def __init__(self, default_model: str, allowed_models: tuple[str, ...] = (), default_temperature: float | None = None, default_max_tokens: int | None = None) -> None:
        self._default_model = default_model
        self._allowed = frozenset(allowed_models)
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens

    def select(self, request: ModelSelectionRequest) -> ModelSelection:
        # 参数兜底双源合一：请求携带的默认值优先（调用方显式传入），否则用构造器注入的全局默认
        default_temperature = request.default_temperature if request.default_temperature is not None else self._default_temperature
        default_max_tokens = request.default_max_tokens if request.default_max_tokens is not None else self._default_max_tokens
        bound = (request.snapshot_model or "").strip()
        if bound:
            if not self._allowed or bound in self._allowed:
                return ModelSelection(
                    model=bound,
                    source=SELECTION_SOURCE_SNAPSHOT,
                    temperature=_or_default(request.snapshot_temperature, default_temperature),
                    max_tokens=_or_default(request.snapshot_max_tokens, default_max_tokens),
                )
            return ModelSelection(
                model=self._default_model,
                source=SELECTION_SOURCE_WHITELIST_FALLBACK,
                reason=f"snapshot model {bound!r} is not in the allowed list",
                temperature=default_temperature,
                max_tokens=default_max_tokens,
            )
        return ModelSelection(
            model=self._default_model,
            source=SELECTION_SOURCE_DEFAULT,
            temperature=default_temperature,
            max_tokens=default_max_tokens,
        )


class RuleBasedModelSelectorPlugin:
    manifest = PluginManifest(
        name="rule_based_model_selector",
        description="按 AgentVersion 快照绑定的规则模型选择器（绑定直通 → 白名单校验 → 默认回退；参数同步快照优先/全局兜底）",
        provides=("model.selector",),
        requires=("model.provider",),
    )

    def contribute(self, sdk) -> None:
        settings = sdk.settings
        selector = RuleBasedModelSelector(
            settings.model_name,
            tuple(settings.model_allowed_names),
            default_temperature=getattr(settings, "model_temperature", None),
            default_max_tokens=getattr(settings, "model_max_tokens", None),
        )
        sdk.register_port("model.selector", lambda: selector, label="rule_based_model_selector")
