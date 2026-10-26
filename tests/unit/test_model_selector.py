"""rule-based-model-selector（model.selector Port）单元测试（文档 §6.1）。

覆盖交付要点：绑定优先于默认、快照不被热改、白名单校验、默认回退、装配解析。
"""

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine

from app.assembly import assemble
from app.builtin_plugins.manifest import PluginSdk
from app.builtin_plugins.model_selector import RuleBasedModelSelector, RuleBasedModelSelectorPlugin
from app.kernel.ports.model_selector import (
    SELECTION_SOURCE_DEFAULT,
    SELECTION_SOURCE_SNAPSHOT,
    SELECTION_SOURCE_WHITELIST_FALLBACK,
    ModelSelectionRequest,
    resolve_model,
    resolve_model_selection,
    snapshot_model_params,
)
from app.kernel.ports.specs import PORT_SPECS
from app.kernel.registry.port_registry import PortRegistry, PortState


def _request(snapshot_model: str | None, default: str = "mock", *, snapshot_temperature=None, snapshot_max_tokens=None, default_temperature=None, default_max_tokens=None) -> ModelSelectionRequest:
    return ModelSelectionRequest(agent_version_id=uuid4(), snapshot_model=snapshot_model, default_model=default, snapshot_temperature=snapshot_temperature, snapshot_max_tokens=snapshot_max_tokens, default_temperature=default_temperature, default_max_tokens=default_max_tokens)


def test_snapshot_binding_wins_over_default():
    selector = RuleBasedModelSelector(default_model="default-model")
    selection = selector.select(_request("gpt-x"))
    assert selection.model == "gpt-x"
    assert selection.source == SELECTION_SOURCE_SNAPSHOT


def test_snapshot_not_affected_by_global_reconfiguration():
    # 快照语义：同一冻结绑定，在全局默认模型变更前后选择结果一致（文档 §6.1）
    request = _request("gpt-x")
    assert RuleBasedModelSelector(default_model="old-default").select(request).model == "gpt-x"
    assert RuleBasedModelSelector(default_model="new-default").select(request).model == "gpt-x"


def test_no_binding_falls_back_to_default():
    selection = RuleBasedModelSelector(default_model="default-model").select(_request(None))
    assert selection.model == "default-model"
    assert selection.source == SELECTION_SOURCE_DEFAULT


def test_blank_binding_falls_back_to_default():
    selection = RuleBasedModelSelector(default_model="default-model").select(_request("   "))
    assert selection.model == "default-model"
    assert selection.source == SELECTION_SOURCE_DEFAULT


def test_whitelist_allows_bound_model():
    selector = RuleBasedModelSelector(default_model="default-model", allowed_models=("gpt-x", "gpt-y"))
    selection = selector.select(_request("gpt-x"))
    assert selection.model == "gpt-x"
    assert selection.source == SELECTION_SOURCE_SNAPSHOT


def test_whitelist_rejects_unknown_binding():
    selector = RuleBasedModelSelector(default_model="default-model", allowed_models=("gpt-y",))
    selection = selector.select(_request("gpt-x"))
    assert selection.model == "default-model"
    assert selection.source == SELECTION_SOURCE_WHITELIST_FALLBACK
    assert selection.reason is not None and "gpt-x" in selection.reason


def test_resolve_model_without_selector_returns_default():
    # 消费方兜底：选择器缺失（如旧构造路径）时保持旧行为
    assert resolve_model(None, agent_version_id=None, snapshot_model="gpt-x", default_model="default-model") == "default-model"


def test_snapshot_params_win_over_global_defaults():
    # 快照语义：温度/max_tokens 逐键快照优先（发布后不随全局配置漂移）
    selector = RuleBasedModelSelector(default_model="default-model", default_temperature=0.9, default_max_tokens=4096)
    selection = selector.select(_request("gpt-x", snapshot_temperature=0.2, snapshot_max_tokens=256))
    assert selection.temperature == 0.2
    assert selection.max_tokens == 256


def test_missing_snapshot_param_falls_back_to_global_default():
    # 快照未设置的键回退全局兜底（逐键独立回退）
    selector = RuleBasedModelSelector(default_model="default-model", default_temperature=0.9, default_max_tokens=4096)
    selection = selector.select(_request("gpt-x", snapshot_temperature=None, snapshot_max_tokens=256))
    assert selection.temperature == 0.9
    assert selection.max_tokens == 256


def test_no_binding_params_use_global_defaults():
    selection = RuleBasedModelSelector(default_model="default-model", default_temperature=0.9, default_max_tokens=4096).select(_request(None))
    assert selection.temperature == 0.9
    assert selection.max_tokens == 4096


def test_whitelist_fallback_also_falls_back_params():
    # 白名单拦截时：模型与参数一并回退全局默认（不允许快照参数绕过白名单生效）
    selector = RuleBasedModelSelector(default_model="default-model", allowed_models=("gpt-y",), default_temperature=0.9, default_max_tokens=4096)
    selection = selector.select(_request("gpt-x", snapshot_temperature=0.2, snapshot_max_tokens=256))
    assert selection.source == SELECTION_SOURCE_WHITELIST_FALLBACK
    assert selection.temperature == 0.9
    assert selection.max_tokens == 4096


def test_resolve_model_selection_without_selector_returns_defaults():
    selection = resolve_model_selection(None, agent_version_id=None, snapshot_model="gpt-x", default_model="default-model", default_temperature=0.9, default_max_tokens=4096)
    assert selection.model == "default-model"
    assert selection.temperature == 0.9
    assert selection.max_tokens == 4096


def test_snapshot_model_params_helper_per_key_fallback():
    # 无选择器消费路径（chat 直答/流式）：与选择器语义一致的逐键兜底
    temperature, max_tokens = snapshot_model_params({"temperature": 0.2}, default_temperature=0.9, default_max_tokens=4096)
    assert temperature == 0.2
    assert max_tokens == 4096
    assert snapshot_model_params(None, default_temperature=0.9, default_max_tokens=4096) == (0.9, 4096)
    # 快照显式 null 视为未设置 → 回退全局默认
    assert snapshot_model_params({"temperature": None, "max_tokens": None}, default_temperature=0.9, default_max_tokens=4096) == (0.9, 4096)


def test_plugin_injects_global_param_defaults():
    spec = next(s for s in PORT_SPECS if s.name == "model.selector")
    registry = PortRegistry((spec,))
    settings = SimpleNamespace(model_name="cfg-model", model_allowed_names=[], model_temperature=0.5, model_max_tokens=2048)
    RuleBasedModelSelectorPlugin().contribute(PluginSdk.of(RuleBasedModelSelectorPlugin(), settings=settings, registry=registry))
    registry.resolve()
    selector = registry.get("model.selector")
    selection = selector.select(_request("cfg-model"))
    assert selection.temperature == 0.5
    assert selection.max_tokens == 2048


def test_plugin_registers_and_resolves():
    spec = next(s for s in PORT_SPECS if s.name == "model.selector")
    registry = PortRegistry((spec,))
    settings = SimpleNamespace(model_name="cfg-model", model_allowed_names=["cfg-model"])
    RuleBasedModelSelectorPlugin().contribute(PluginSdk.of(RuleBasedModelSelectorPlugin(), settings=settings, registry=registry))
    registry.resolve()
    resolution = registry.resolution("model.selector")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "rule_based_model_selector"
    selector = registry.get("model.selector")
    assert selector.select(_request(None, default="cfg-model")).model == "cfg-model"
    assert selector.select(_request("cfg-model")).model == "cfg-model"


def test_community_assembly_resolves_model_selector(tmp_path):
    # 装配冒烟：community Profile 下 model.selector 经 requires 链校验并解析成功
    settings = SimpleNamespace(
        model_base_url=None,
        model_api_key=None,
        model_name="mock",
        model_allowed_names=[],
        file_storage_dir=tmp_path / "files",
        # v1.3：沙箱 cwd 白名单在装配期强制校验，轻量 settings 必须显式给出
        sandbox_allowed_roots=[str(tmp_path / "skills")],
    )
    result = assemble(settings, create_engine("sqlite://"))
    assert result.registry.resolution("model.selector").state == PortState.RESOLVED


def test_effective_max_tokens_precedence():
    """模型档案 max_output_tokens 兜底（模型页配置生效链路）：

    快照显式值 > 模型档案上限（ENABLED 且 >0，按租户隔离）> 全局默认。
    """
    from uuid import UUID

    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 确保全部表注册到 metadata
    from app.db.base import Base
    from app.models import ModelResource
    from app.services.model_limits import effective_max_tokens

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    tenant = UUID(int=1)

    def add_model(identifier: str, max_output: int | None, status: str = "ENABLED") -> None:
        db.add(ModelResource(tenant_id=tenant, provider_id=uuid4(), name=identifier, model_identifier=identifier, status=status, max_output_tokens=max_output, config={}))

    add_model("m1", 8192)
    add_model("m-disabled", 999, status="DISABLED")
    add_model("m-zero", 0)
    db.commit()

    # 快照显式值最高优先（智能体版本逐键配置语义不变）
    assert effective_max_tokens(db, tenant_id=tenant, model_identifier="m1", snapshot_max_tokens=512, default_max_tokens=4096) == 512
    # 模型页配置兜底
    assert effective_max_tokens(db, tenant_id=tenant, model_identifier="m1", snapshot_max_tokens=None, default_max_tokens=4096) == 8192
    # 无匹配档案 → 全局默认
    assert effective_max_tokens(db, tenant_id=tenant, model_identifier="missing", snapshot_max_tokens=None, default_max_tokens=4096) == 4096
    # 租户隔离：他租户档案不生效
    assert effective_max_tokens(db, tenant_id=UUID(int=2), model_identifier="m1", snapshot_max_tokens=None, default_max_tokens=4096) == 4096
    # 停用档案不生效
    assert effective_max_tokens(db, tenant_id=tenant, model_identifier="m-disabled", snapshot_max_tokens=None, default_max_tokens=4096) == 4096
    # 非正值视为未配置
    assert effective_max_tokens(db, tenant_id=tenant, model_identifier="m-zero", snapshot_max_tokens=None, default_max_tokens=4096) == 4096
