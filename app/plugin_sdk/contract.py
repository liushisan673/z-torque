"""插件契约本体：manifest / PluginSdk / 装配期校验（标准插件的唯一授权面）。

设计要点（契约 §5 五条纪律）：
- manifest 唯一入口：声明 apiVersion / provides / requires / permissions；
- 插件只实现内核已定义的 Port（registry 对未知 Port 直接拒绝）；
- 插件只经 SDK 读写状态：配置、Port 注册、共享对象、受控数据库会话、审计读写，
  全部经本模块，插件不持有装配上下文内部结构，也不 import 内核实现模块；
- 裸 engine 仅对声明 db.query 权限的插件开放（契约 §5.4 权限声明式）。

**可移植性约束**：本文件即"标准插件可以依赖的全部 app.* 面"（连同 app.plugin_sdk
的其余再导出与 app.core 公共原语）。企业版复用插件时只需本包 + 内核提供同名能力，
插件源码不出现 `app.kernel.*` / `app.models.*` / `app.services.*` / `app.api.*`
（由 tests/unit/test_plugin_redlines.py 机器校验，只减不增）。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from app.kernel.registry.port_registry import AssemblyError, PortPriority

PLUGIN_API_VERSION = "zsoftym.ai/plugin.v1"

# 内核当前接受的 apiVersion 集合（装配期强校验；企业版升级契约时在此追加）
SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({PLUGIN_API_VERSION})

# 访问裸数据库引擎所需的权限（契约 §5.4：未声明即拒绝）
ENGINE_PERMISSION = "db.query"
# 审计事件只读投影所需的权限（v1.3 §3.7：read_audit 能力单独声明）
AUDIT_READ_PERMISSION = "audit.read"

# 装配上下文共享键：skill.executor Port 的实现实例（跨插件挂载 stdio 注册表用）
SKILL_GATEWAY_KEY = "__skill_gateway__"
# 装配上下文共享键：插件级熔断/配额注册表（全装配期唯一实例）
PLUGIN_GUARD_KEY = "__plugin_guard__"


@dataclass(frozen=True)
class PluginManifest:
    name: str
    description: str
    provides: tuple[str, ...]
    requires: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    api_version: str = PLUGIN_API_VERSION
    version: str = "0.1.0"
    config_schema: dict[str, Any] | None = None
    # 审计事件流只读能力（v1.3 §3.7 / B-12）：默认关闭，需显式声明并在
    # 各插件的 validate_plugin 中校验"附属能力不得凭空获得"，导出/归档插件必须持此能力
    read_audit: bool = False


class PluginSdk:
    """插件 SDK：插件 contribute(sdk) 的唯一授权接口（契约 §5.3/§5.4）。

    - settings：配置只读引用（插件不得修改运行时配置）；
    - register_port：Port 注册（插件归属自动标注，防伪造）；
    - publish / shared：装配期对象共享（替代裸 objects 字典直改）；
    - engine / session：受控数据库访问，仅声明 db.query 权限的插件可用；
    - guard：内核提供的插件级熔断/配额实例（插件不自行构造，语义见契约 §5.5）；
    - read_audit / record_audit：审计只读投影与自证审计写入（插件不 import 内核审计实现）；
    - expose_skill_gateway / attach_stdio_registry：装配产出物挂载。
    """

    def __init__(self, *, settings: Any = None, engine: Any = None, registry: Any = None, objects: dict[str, Any] | None = None, manifest: PluginManifest | None = None, builtin: bool = True, guard: Any = None) -> None:
        self._settings = settings
        self._engine = engine
        self._registry = registry
        self._objects = objects if objects is not None else {}
        self._builtin = builtin
        # 直构/测试场景未传 manifest 时使用宽松占位（生产装配始终传真实 manifest）
        self._manifest = manifest or PluginManifest(name="adhoc", description="", provides=(), permissions=(ENGINE_PERMISSION,))
        self._guard = guard
        self._skill_gateway: Any = None
        self._stdio_registry: Any = None

    @classmethod
    def of(cls, plugin: Any, *, settings: Any = None, engine: Any = None, registry: Any = None, objects: dict[str, Any] | None = None, builtin: bool = True) -> "PluginSdk":
        """按插件 manifest 构造 SDK（测试/直构便利；生产装配由 assembly 逐插件构造）。"""
        return cls(settings=settings, engine=engine, registry=registry, objects=objects, manifest=plugin.manifest, builtin=builtin)

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def registry(self) -> Any:
        """Port 注册表（只读引用；注册经 register_port 以保证插件归属标注）。"""
        return self._registry

    @property
    def engine(self) -> Any:
        if ENGINE_PERMISSION not in self._manifest.permissions:
            raise PermissionError(f"plugin {self._manifest.name} lacks '{ENGINE_PERMISSION}' permission; engine access is denied (契约 §5.4)")
        return self._engine

    @property
    def plugin_name(self) -> str:
        return self._manifest.name

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    def require_permission(self, permission: str) -> None:
        """声明式权限校验：未在 manifest.permissions 声明即拒绝（契约 §5.4）。"""
        if permission not in self._manifest.permissions:
            raise PermissionError(f"plugin {self._manifest.name} lacks '{permission}' permission (契约 §5.4)")

    @property
    def guard(self) -> Any:
        """插件级熔断/配额实例（契约 §5.5）。

        由装配根注入（全装配期唯一），未注入时按 settings 惰性构造并写回共享上下文，
        保证同一装配内所有插件看到同一实例；插件不应自行 `from ... import` 该类。
        """
        if self._guard is None:
            self._guard = self._objects.get(PLUGIN_GUARD_KEY)
        if self._guard is None:
            from app.kernel.plugin_guard import PluginGuardRegistry

            self._guard = PluginGuardRegistry.from_settings(self._settings)
            self._objects[PLUGIN_GUARD_KEY] = self._guard
        return self._guard

    @contextmanager
    def session(self) -> Iterator[Any]:
        """受控短事务会话（v1.3 §3.7 / B-12）。

        - 需声明 db.query 权限；
        - 成功提交、异常回滚、始终关闭，插件不接触引擎生命周期；
        - 后台线程（导出/归档任务）不共享请求会话，由插件在各自线程内开新会话。
        """
        from sqlalchemy.orm import Session

        self.require_permission(ENGINE_PERMISSION)
        if self._engine is None:
            raise PermissionError(f"plugin {self._manifest.name} has no database engine bound")
        db = Session(bind=self._engine, expire_on_commit=False)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def read_audit(self, query: Any) -> Iterator[Any]:
        """审计事件只读投影（仅 manifest.read_audit=True 的插件可用）。

        返回 keyset 分页生成器；调用方按需截断（导出达到 max_rows 即停止）。
        """
        from app.kernel.audit.reader import iter_audit_events

        if not self._manifest.read_audit:
            raise PermissionError(f"plugin {self._manifest.name} lacks audit read capability; declare manifest.read_audit (v1.3 §3.7)")
        return iter_audit_events(self.session, query)

    def read_published_agents(self, *, tenant_id: Any, limit: int = 50) -> list[dict[str, Any]]:
        """已发布 Agent 的只读投影（内核自有表的唯一读缝，a2a 本地发现用）。

        插件只声明 db.query 权限即可读取；返回纯 dict，会话关闭后仍可用。
        """
        from app.kernel.plugin_reads import list_published_agents

        self.require_permission(ENGINE_PERMISSION)
        return list_published_agents(self.session, tenant_id=tenant_id, limit=limit)

    def list_tenant_ids(self) -> list[Any]:
        """全部租户 ID（定时归档按租户逐个触发；只读）。"""
        from app.kernel.plugin_reads import list_tenant_ids

        self.require_permission(ENGINE_PERMISSION)
        return list_tenant_ids(self.session)

    def record_audit(
        self,
        *,
        tenant_id: Any,
        action: str,
        resource_type: str,
        resource_id: Any = None,
        actor_user_id: Any = None,
        summary: str,
        detail: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        """插件自证审计写入（独立事务，拒绝/异常路径留痕用）。

        审计事件流不可关闭（契约不变量4）：插件被拒绝或失败时，业务事务会回滚，
        若与业务同事务写入必然丢失，故走独立短事务。

        - 需声明 db.query 权限（插件自有引擎）；
        - 写入失败只记日志，不得阻断原本的拒绝响应；
        - 仅用于"插件自身动作"留痕，审计事件流本身仍只经 read_audit 读取。
        """
        import logging

        from app.kernel.audit.service import record_audit

        self.require_permission(ENGINE_PERMISSION)
        try:
            with self.session() as db:
                record_audit(
                    db,
                    tenant_id=tenant_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    actor_user_id=actor_user_id,
                    summary=summary,
                    detail=detail,
                    trace_id=trace_id,
                )
        except Exception:  # pragma: no cover - 审计写入失败不得影响主流程
            logging.getLogger(__name__).warning(
                "plugin %s failed to record audit event %s", self._manifest.name, action, exc_info=True
            )

    def register_port(self, port_name: str, factory: Any, *, priority: int | None = None, label: str) -> None:
        """注册 Port：内置插件默认 BUILTIN；第三方插件默认 PLUGIN。

        契约 §3「显式配置 > 插件优先级 > 内置默认」因此无需插件手写 priority
        即可覆盖内置实现（P2-15）。
        """
        resolved_priority = priority if priority is not None else (PortPriority.BUILTIN if self._builtin else PortPriority.PLUGIN)
        self._registry.register(port_name, factory, priority=resolved_priority, label=label, plugin=self._manifest.name)

    def publish(self, key: str, value: Any) -> None:
        self._objects[key] = value

    def shared(self, key: str, default: Any = None) -> Any:
        return self._objects.get(key, default)

    def expose_skill_gateway(self, gateway: Any) -> None:
        self._skill_gateway = gateway
        # 跨插件共享（契约 §5.3）：SkillGateway 是 skill.executor Port 的实现体，
        # 后加载的插件（如 mcp_connector）需要把装配产出挂到同一个实例上。
        # 每个插件持有独立 SDK，单靠实例字段无法跨插件传递，故同步写入装配上下文。
        self._objects[SKILL_GATEWAY_KEY] = gateway

    @property
    def skill_gateway(self) -> Any:
        return self._skill_gateway

    def attach_stdio_registry(self, registry: Any) -> None:
        """挂载 stdio 传输注册表：同时写入装配产出与已暴露的 SkillGateway。

        mcp_connector 与 skill_executors 由不同插件各自持有 SDK，本 SDK 的
        `_skill_gateway` 通常为 None；此时回退到装配上下文中的共享实例，
        否则 stdio 工具在执行期永远取不到注册表（P0-1）。
        """
        self._stdio_registry = registry
        gateway = self._skill_gateway if self._skill_gateway is not None else self._objects.get(SKILL_GATEWAY_KEY)
        if gateway is not None:
            gateway.stdio_registry = registry

    @property
    def stdio_registry(self) -> Any:
        return self._stdio_registry


# 历史别名：装配上下文即插件 SDK 的视图（插件 contribute 形参类型）
AssemblyContext = PluginSdk


class BuiltinPlugin(Protocol):
    manifest: PluginManifest

    def contribute(self, sdk: PluginSdk) -> None: ...


def validate_manifest(manifest: PluginManifest, known_ports: set[str]) -> None:
    unknown_provides = [name for name in manifest.provides if name not in known_ports]
    if unknown_provides:
        raise ValueError(f"plugin {manifest.name} provides unknown ports: {unknown_provides}")
    unknown_requires = [name for name in manifest.requires if name not in known_ports]
    if unknown_requires:
        raise ValueError(f"plugin {manifest.name} requires unknown ports: {unknown_requires}")
    # 附属能力不得凭空获得（v1.3 §3.7 / B-12）：read_audit 必须伴随显式 audit.read 声明
    if manifest.read_audit and AUDIT_READ_PERMISSION not in manifest.permissions:
        raise ValueError(f"plugin {manifest.name} declares read_audit but lacks '{AUDIT_READ_PERMISSION}' permission")
    # apiVersion 兼容性（标准插件可移植性）：内核只接受自己认识的契约版本，
    # 未知版本直接拒绝启动，避免"装上但语义半懂"的静默错配。
    if manifest.api_version not in SUPPORTED_API_VERSIONS:
        raise ValueError(
            f"plugin {manifest.name} declares unsupported api_version {manifest.api_version!r} "
            f"(kernel supports {sorted(SUPPORTED_API_VERSIONS)})"
        )
    # 结构化 config_schema 必须是合法的 JSON Schema（历史描述性映射不校验）
    if is_structured_config_schema(manifest.config_schema):
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import Draft202012Validator

        try:
            Draft202012Validator.check_schema(manifest.config_schema)
        except SchemaError as exc:
            raise ValueError(f"plugin {manifest.name} has an invalid config_schema: {exc.message}") from exc


# ---- 插件配置契约（阶段 2）----
# config_schema 支持两种形态：
# 1. **结构化（推荐；标准插件必须）**：JSON Schema（含 type/properties 等关键字）。
#    装配期按 Settings 投影后校验，缺必填 / 类型不符 / 枚举越界即 AssemblyError。
# 2. **描述性（历史兼容）**：{key: "说明字符串"}，只作文档，不参与校验（21 个旧插件）。
#
# 边界：schema **只做形状校验**（类型/枚举/必填），语义校验仍归各插件自己的
# `config.validate()` —— 不引入第二套配置中心，也不与既有 validate 重复判断。
_STRUCTURED_SCHEMA_KEYS = ("type", "properties", "$schema", "required", "additionalProperties")


def is_structured_config_schema(schema: dict[str, Any] | None) -> bool:
    """是否为 JSON Schema 形态（而非历史的 {key: 描述} 映射）。"""
    return isinstance(schema, dict) and any(key in schema for key in _STRUCTURED_SCHEMA_KEYS)


def project_plugin_config(manifest: PluginManifest, settings: Any) -> dict[str, Any]:
    """把扁平 Settings 投影成插件配置 dict（只取 schema 声明的键）。

    投影规则（与「Schema 描述 + 投影到现有 Settings」一致，不另建配置源）：
    - Settings 有同名属性 → 取属性值（含 pydantic 默认值）；
    - 属性不存在但 schema 给了 default → 用 default（轻量/替身 settings 场景）；
    - 两者都没有 → 不放入，交给 `required` 判定。
    """
    schema = manifest.config_schema or {}
    if not is_structured_config_schema(schema):
        return {key: getattr(settings, key, None) for key in schema}

    projected: dict[str, Any] = {}
    for key, spec in (schema.get("properties") or {}).items():
        if hasattr(settings, key):
            projected[key] = getattr(settings, key)
        elif isinstance(spec, dict) and "default" in spec:
            projected[key] = spec["default"]
    return projected


def validate_plugin_config(manifest: PluginManifest, settings: Any) -> dict[str, Any]:
    """装配期校验插件配置，返回投影结果（供 capabilities/诊断使用）。

    仅对结构化 schema 生效；历史描述性映射原样返回，行为不变。
    """
    schema = manifest.config_schema
    if not is_structured_config_schema(schema):
        return project_plugin_config(manifest, settings)

    from jsonschema.validators import Draft202012Validator

    config = project_plugin_config(manifest, settings)
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(e.absolute_path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors
        )
        # 与插件自身的 config.validate() 失败同一出口：装配期 fail fast
        raise AssemblyError(f"plugin {manifest.name} has invalid configuration: {details}")
    return config


# ---- Module 装配层（契约 §4：Profile → Module → Plugin）----


@dataclass(frozen=True)
class ModuleSpec:
    """Module：承载一个相对完整的业务/基础能力域，声明构成插件与所需 Port。

    plugins 顺序即展开后 contribute 顺序；requires_ports 在装配 resolve 后校验
    （缺失抛 AssemblyError 拒绝启动），实现"Module 声明需要哪些 Port"。
    """

    name: str
    description: str
    plugins: tuple[str, ...] = ()
    requires_ports: tuple[str, ...] = ()


def validate_module(spec: ModuleSpec, known_plugins: dict[str, Any]) -> None:
    unknown = [name for name in spec.plugins if name not in known_plugins]
    if unknown:
        raise ValueError(f"module {spec.name} references unknown plugins: {unknown}")
