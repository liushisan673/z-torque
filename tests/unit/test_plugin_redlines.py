"""v1.3 红线的机器化静态校验（§3.7 / §4.5 / §5.7 / §6.8）。

红线只写进文档等于没有：这些断言直接扫源码文本，任何"顺手加一行"的越界
都会在 CI 里红掉。规则刻意用"缺了才报"的正向断言（例如必须有 killpg），
避免整文件黑名单带来的误报。
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
AUDIT_PLUGIN = APP / "builtin_plugins" / "audit_export_archive"
A2A_PLUGIN = APP / "builtin_plugins" / "a2a_gateway"


def _py_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _concat(files: list[Path]) -> str:
    return "\n".join(_read(path) for path in files)


# ---- 分层：内核 / 服务 / 模型 不得反向依赖插件包 ----
# 只匹配真实的 import 语句：docstring 里"禁止导入 app.builtin_plugins"这类
# 约束说明本身不应被判违规（否则红线文档与红线校验互相打架）。
_IMPORT_PLUGIN_RE = re.compile(r"^\s*(?:from|import)\s+app\.builtin_plugins", re.MULTILINE)


def test_inner_layers_never_import_plugin_package():
    offenders: list[str] = []
    for layer in ("kernel", "services", "core", "models", "ws", "security"):
        for path in _py_files(APP / layer):
            if _IMPORT_PLUGIN_RE.search(_read(path)):
                offenders.append(str(path.relative_to(APP.parent)))
    assert offenders == [], f"内核/服务/模型层不得 import 插件包: {offenders}"


def test_plugin_packages_never_import_http_layer():
    offenders = [str(p) for p in _py_files(AUDIT_PLUGIN) + _py_files(A2A_PLUGIN) if "app.api" in _read(p)]
    assert offenders == [], f"插件不得依赖 API 层: {offenders}"


# ---- 数据边界：插件只用 sdk.session / sdk.read_audit，不碰审计写入 ----
def test_audit_plugin_never_touches_audit_log_directly():
    text = _concat(_py_files(AUDIT_PLUGIN))
    for forbidden in ("zr_audit_log", "AuditLog", "SessionLocal", "create_engine"):
        assert forbidden not in text, f"audit_export_archive 不得直接访问审计存储：{forbidden}"
    # 裸 SQL 只认 `text("..."` / `text(f"..."`，避免误伤 redact_text( 之类的自定义函数
    assert re.search(r"\btext\(\s*[f]?[\"']", text) is None, "插件不得拼裸 SQL"
    assert "sdk.read_audit" in text or "self._sdk.read_audit" in text


def test_plugins_never_read_env_or_own_lifecycle_hooks():
    text = _concat(_py_files(AUDIT_PLUGIN) + _py_files(A2A_PLUGIN))
    for forbidden in ("os.environ", "getenv", "atexit", "signal.signal", "sys.exit"):
        assert forbidden not in text, f"插件不得自行读环境变量或接管进程生命周期：{forbidden}"


def test_audit_plugin_has_no_outbound_network():
    text = _concat(_py_files(AUDIT_PLUGIN))
    for forbidden in ("httpx", "requests.", "urllib.request", "socket."):
        assert forbidden not in text, f"审计导出插件不得出站：{forbidden}"


# ---- 沙箱：cwd 白名单 / rlimit / 进程组终止 / 元信息上报 ----
def test_sandbox_driver_enforces_hardening_mechanics():
    # 加固实现归插件（原内核 md_skill_runtime.py，已下沉到 sandbox_driver/process.py）
    text = _read(APP / "builtin_plugins" / "sandbox_driver" / "process.py")
    assert "shell=True" not in text, "沙箱禁止 shell=True（命令注入面）"
    assert "killpg" in text, "POSIX 必须按进程组终止，避免子进程逃逸"
    assert "start_new_session" in text, "子进程必须独立会话/进程组"
    assert "setrlimit" in text, "POSIX 必须落地 rlimit（CPU/FSIZE/NPROC）"
    assert "_emit_sandbox_meta" in text, "必须向 observability.sink 上报沙箱元信息"
    assert "AI_SANDBOX_RESOURCE_EXCEEDED" in text, "rlimit 命中必须映射为资源超限错误码"


def test_sandbox_plugin_fails_fast_on_empty_allowlist():
    text = _read(APP / "builtin_plugins" / "sandbox_driver" / "plugin.py")
    assert "AssemblyError" in text
    assert "allowed_roots" in text
    # 空白名单必须拒绝装配，而不是退化为"不限制"
    assert "non-empty sandbox_allowed_roots" in text


# ---- 导出：脱敏与递归防护 ----
def test_exporter_uses_kernel_redaction_and_guards_recursion():
    text = _read(AUDIT_PLUGIN / "exporter.py")
    assert "redact_mapping" in text, "detail 必须复用内核脱敏实现"
    config_text = _read(AUDIT_PLUGIN / "config.py")
    assert "SELF_ACTION_PREFIX" in config_text and '"AUDIT_"' in config_text


def test_export_job_row_limit_is_a_failure_not_truncation():
    text = _read(AUDIT_PLUGIN / "jobs.py")
    assert "AI_AUDIT_EXPORT_TOO_LARGE" in text
    assert "truncated=False" in text, "超限必须失败，不得产出 truncated=true 的导出件"


# ---- A2A：认证/鉴权在 identity，路由不触 DB ----
def test_a2a_gateway_module_is_db_free():
    text = _read(A2A_PLUGIN / "gateway.py")
    # 只允许经 sdk.session() 拿到受门禁的短事务；不得自建引擎/会话，也不做出站
    for forbidden in ("app.models", "SessionLocal", "create_engine", "select(", "httpx"):
        assert forbidden not in text, f"gateway.py 只做编排，不得直接访问：{forbidden}"
    assert "self._sdk.session()" in text


def test_a2a_identity_requires_permission_and_rejects_header_trust():
    text = _read(A2A_PLUGIN / "identity.py")
    assert "has_all" in text, "入站必须校验 a2a.invoke 权限"
    assert "X-User-Id" in text, "必须显式说明不信任裸身份头"
    # 拒绝路径必须留痕（独立事务），且只能经 SDK（插件不得 import 内核审计实现）
    assert "sdk.record_audit" in text, "拒绝路径必须经 sdk.record_audit 留痕"
    assert "kernel.audit" not in text


# ---- 标准插件可移植性：插件只经 SDK，不 import 内核实现 / 模型 / 服务 ----
# 目标形态：三个插件目录可整体拷到企业版，只补一个注册项即可装配。
# 因此插件源码里的 app.* 依赖必须收窄到「公开契约」，白名单刻意保持极窄：
#   1. SDK 本体 app.builtin_plugins.manifest（PluginManifest / PluginSdk）
#   2. 未来的公开契约包 app.plugin_sdk（AuditQuery / 错误码等再导出口）
#   3. 共享原语 app.core.*（errors / ids / servertime / redaction，企业版必然同源）
# 其余一律视为内核实现细节（kernel / models / services / api / ws / security）。
_PLUGIN_SOURCES: tuple[Path, ...] = (
    AUDIT_PLUGIN,
    A2A_PLUGIN,
    APP / "builtin_plugins" / "sandbox_driver",
)
_ALLOWED_APP_PREFIXES: tuple[str, ...] = (
    "app.builtin_plugins.manifest",
    "app.plugin_sdk",
    "app.core.",
)
_APP_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(app[\w.]*)", re.MULTILINE)

# 现状基线（只减不增）：每修掉一处就从这里删一行；新增越界立即红。
# 清单本身是工单，machine-readable，避免"文档写了但没人执行"。
KNOWN_OFFENDERS: frozenset[str] = frozenset()


def _plugin_py_files() -> list[Path]:
    files: list[Path] = []
    for root in _PLUGIN_SOURCES:
        if root.is_dir():
            files.extend(_py_files(root))
        elif root.is_file():
            files.append(root)
    return sorted(files)


def _self_prefix(path: Path) -> str:
    """插件自身的包前缀（插件内部互相 import 不算越界）。"""
    if path.parent.name == "builtin_plugins":
        return f"app.builtin_plugins.{path.stem}"
    return f"app.builtin_plugins.{path.parent.name}"


def _plugin_import_violations() -> list[str]:
    violations: list[str] = []
    for path in _plugin_py_files():
        self_prefix = _self_prefix(path)
        rel = path.relative_to(APP.parent).as_posix()
        for match in _APP_IMPORT_RE.finditer(_read(path)):
            module = match.group(1).rstrip(".")
            # `from app.builtin_plugins import x` 视为包级 import，按前缀补全判断
            if module == "app.builtin_plugins":
                module = f"{module}.{path.parent.name}"
            if module.startswith(_ALLOWED_APP_PREFIXES) or module.startswith(self_prefix):
                continue
            line_no = _read(path)[: match.start()].count("\n") + 1
            violations.append(f"{rel}:{line_no} -> {module}")
    return sorted(violations)


def test_plugins_import_only_public_sdk_surface():
    current = set(_plugin_import_violations())
    known = set(KNOWN_OFFENDERS)
    regressions = sorted(current - known)
    leftovers = sorted(current & known)
    stale = sorted(known - current)
    detail = "\n".join(
        [
            f"当前越界 {len(current)} 处（基线 {len(known)} 处，未消 {len(leftovers)} 处）：",
            *(f"  - {item}" for item in leftovers),
            *(f"  + [新增] {item}" for item in regressions),
            *(f"  - [已修] {item}（请从 KNOWN_OFFENDERS 删掉此行）" for item in stale),
        ]
    )
    assert not regressions, f"插件包出现新的内核依赖（标准插件必须只经 SDK）：\n{detail}"
    assert not stale, f"KNOWN_OFFENDERS 已过期，请同步删除已修项：\n{detail}"


def test_a2a_peer_whitelist_and_https_enforced():
    peers = _read(A2A_PLUGIN / "peers.py")
    config = _read(A2A_PLUGIN / "config.py")
    assert "network.outbound" in peers, "每次出站前必须自证 network.outbound"
    assert "follow_redirects=False" in peers, "出站禁止跟随重定向（白名单绕过面）"
    assert "max_response_bytes" in peers, "响应体必须有上限"
    assert "must not be empty when a2a_remote_peers is configured" in config
    assert "must use https" in config
