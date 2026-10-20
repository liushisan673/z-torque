from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID


SELECTION_SOURCE_SNAPSHOT = "snapshot"
SELECTION_SOURCE_DEFAULT = "default"
SELECTION_SOURCE_WHITELIST_FALLBACK = "whitelist_fallback"


@dataclass(frozen=True)
class ModelSelectionRequest:
    """模型选择请求：调用方从 AgentVersion 快照提取绑定后传入（内核不感知 ORM 模型）。

    snapshot_model / snapshot_temperature / snapshot_max_tokens 均为发布时冻结的
    快照值（不存在时为 None，由实现按规则回退全局默认）。
    """

    agent_version_id: UUID | None
    snapshot_model: str | None  # AgentVersion.config_snapshot 冻结的模型绑定（发布后不可热改）
    default_model: str  # 全局默认（settings.model_name）
    snapshot_temperature: float | None = None
    snapshot_max_tokens: int | None = None
    default_temperature: float | None = None
    default_max_tokens: int | None = None


@dataclass(frozen=True)
class ModelSelection:
    model: str
    source: str  # snapshot / default / whitelist_fallback
    reason: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class ModelSelector(ABC):
    """model.selector Port 契约：按 AgentVersion 绑定选择模型与调用参数（文档 §6.1）。

    快照语义：一旦版本发布，其冻结的模型绑定/参数不随全局配置热改而漂移；
    实现负责「绑定直通 → 校验 → 默认回退」的策略，由调用方在 ModelGateway
    调用前查询。具体实现属能力实现，位于 app/builtin_plugins/model_selector.py。
    """

    @abstractmethod
    def select(self, request: ModelSelectionRequest) -> ModelSelection: ...


def _or_default(snapshot_value: Any, default_value: Any) -> Any:
    return snapshot_value if snapshot_value is not None else default_value


def resolve_model_selection(selector: ModelSelector | None, *, agent_version_id: UUID | None, snapshot_model: str | None, default_model: str, snapshot_temperature: float | None = None, snapshot_max_tokens: int | None = None, default_temperature: float | None = None, default_max_tokens: int | None = None) -> ModelSelection:
    """消费方便捷入口：无选择器时模型回退全局默认，快照参数仍逐键优先（快照语义不依赖选择器存在）。"""
    if selector is None:
        return ModelSelection(
            model=default_model,
            source=SELECTION_SOURCE_DEFAULT,
            temperature=snapshot_temperature if snapshot_temperature is not None else default_temperature,
            max_tokens=snapshot_max_tokens if snapshot_max_tokens is not None else default_max_tokens,
        )
    return selector.select(
        ModelSelectionRequest(
            agent_version_id=agent_version_id,
            snapshot_model=snapshot_model,
            default_model=default_model,
            snapshot_temperature=snapshot_temperature,
            snapshot_max_tokens=snapshot_max_tokens,
            default_temperature=default_temperature,
            default_max_tokens=default_max_tokens,
        )
    )


def resolve_model(selector: ModelSelector | None, *, agent_version_id: UUID | None, snapshot_model: str | None, default_model: str) -> str:
    """消费方便捷入口（仅模型名）：无选择器时直接回退默认模型（保持旧行为）。"""
    if selector is None:
        return default_model
    return selector.select(ModelSelectionRequest(agent_version_id=agent_version_id, snapshot_model=snapshot_model, default_model=default_model)).model


def snapshot_model_params(snapshot: dict[str, Any] | None, *, default_temperature: float | None, default_max_tokens: int | None) -> tuple[float | None, int | None]:
    """无选择器消费路径的参数兜底：快照键优先（显式 None 视为未设置），否则全局默认。

    供 chat 直答 / 流式入口等不持有 model.selector 的调用方使用，与
    RuleBasedModelSelector 的参数回退语义保持一致。
    """
    cfg = snapshot or {}
    temperature = cfg.get("temperature")
    max_tokens = cfg.get("max_tokens")
    return (
        temperature if temperature is not None else default_temperature,
        max_tokens if max_tokens is not None else default_max_tokens,
    )
