"""社区版插件市场安装器：把「选配插件」装成本地装配清单。

用户视角三步：

    python scripts/zt_plugins.py list                       # 看市场里有什么
    python scripts/zt_plugins.py install audit_export_archive a2a_gateway
    # 重启后端 → 前端即可用

安装器只做四件事，全部可逆：

1. 从 `plugins_market/` 「下载」插件包到 `data/plugins/downloads/` 并校验 sha256；
2. 解压到 `data/plugins/installed/<id>/<version>/`，写 `data/plugins/installed.json`；
3. 跑 `alembic upgrade head`（插件表）与 `python -m app.cli`（补权限种子）；
4. 写装配清单到 `.env`（`ASSEMBLY_PROFILE=community+<已装插件>`），
   写运行开关到 `.env.plugins`（由 `scripts/run_backend.ps1` 注入环境变量）。

设计约束（刻意为之）：
- **只走配置，不改内核代码**：装配清单用 `ASSEMBLY_PROFILE` 的选配写法表达，
  插件实现仍是社区版内置的那份；因此「卸载」= 删块，恢复到安装前的 `community`。
- **装配与启用分开放**：`ASSEMBLY_PROFILE` 才进 `.env`；`A2A_ENABLED` 这类运行开关
  进 `.env.plugins`，因为它们**默认值是契约的一部分**（有测试依赖 false），
  塞进 `.env` 会让"装了插件"悄悄改掉别人机器上的默认行为。
- **fail-fast**：块外若已有同名键（例如用户手写过 `ASSEMBLY_PROFILE`），安装器
  拒绝写入并提示，避免托管块静默覆盖手写值。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MARKET_DIR = ROOT / "plugins_market"
INDEX_PATH = MARKET_DIR / "index.json"
STATE_DIR = ROOT / "data" / "plugins"
DOWNLOAD_DIR = STATE_DIR / "downloads"
INSTALLED_DIR = STATE_DIR / "installed"
STATE_PATH = STATE_DIR / "installed.json"
RUNTIME_ENV_PATH = ROOT / ".env.plugins"

BLOCK_BEGIN = "# >>> zt-plugins:managed（安装器自动维护，卸载时整块删除；勿手改） >>>"
BLOCK_END = "# <<< zt-plugins:managed <<<"
BASE_PROFILE = "community"

# 仅在 import app 失败时兜底：sandbox_driver 属社区档，绝不能重复选配
_FALLBACK_COMMUNITY = {"sandbox_driver"}


class InstallerError(RuntimeError):
    """安装器可预期的失败（打印原因后退出，不抛栈）。"""


def _die(message: str) -> None:
    print(f"\n[失败] {message}", file=sys.stderr)
    raise SystemExit(2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        raise InstallerError(
            f"市场索引不存在：{INDEX_PATH.relative_to(ROOT)}；先运行 python scripts/build_plugin_market.py"
        )
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def _read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"schema": 1, "installed": []}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _write_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _installed_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in state.get("installed", [])}


def _community_plugins() -> set[str]:
    """社区档已装配的插件名（用于判断"装 = 只启用"还是"装 = 追加装配"）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from app.assembly import expand_profile
        from app.builtin_plugins.modules import PROFILE_COMMUNITY

        return set(expand_profile(PROFILE_COMMUNITY))
    except Exception as exc:  # pragma: no cover - 环境异常时用保守集合（宁可多算，不可重复选配）
        print(f"  ! 无法加载社区档插件清单（{exc}），按保守集合处理（sandbox_driver 视为已装配）")
        return set(_FALLBACK_COMMUNITY)


def _download(package: dict[str, Any]) -> Path:
    """模拟下载：复制市场包并校验 sha256（真实安装器在这里是 HTTP GET）。"""
    source = MARKET_DIR / package["file"]
    if not source.exists():
        raise InstallerError(f"市场包缺失：{source.relative_to(ROOT)}")
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != package["sha256"]:
        raise InstallerError(
            f"{package['file']} 校验失败：期望 {package['sha256'][:12]}… 实际 {digest[:12]}…"
        )
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = DOWNLOAD_DIR / package["file"]
    target.write_bytes(payload)
    return target


def _extract(package: dict[str, Any], archive: Path) -> Path:
    target = INSTALLED_DIR / package["id"] / package["version"]
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            # 市场包是本地生成的，仍按"不可信输入"处理：拒绝绝对路径与上跳
            if member.startswith("/") or ".." in Path(member).parts:
                raise InstallerError(f"包内路径非法：{member}")
        zf.extractall(target)
    return target


def _env_block(profile: str) -> list[str]:
    if profile == BASE_PROFILE:
        return []
    return [BLOCK_BEGIN, f"ASSEMBLY_PROFILE={profile}", BLOCK_END]


def _split_env(text: str) -> tuple[list[str], list[str], bool]:
    """把 .env 拆成「托管块外」「托管块内」，并返回是否已存在块。"""
    outside: list[str] = []
    inside: list[str] = []
    seen_block = False
    in_block = False
    for line in text.splitlines():
        marker = line.strip()
        if marker == BLOCK_BEGIN:
            in_block = True
            seen_block = True
            continue
        if marker == BLOCK_END:
            in_block = False
            continue
        (inside if in_block else outside).append(line)
    return outside, inside, seen_block


def _foreign_keys(outside: list[str], managed: set[str]) -> list[str]:
    """块外手写的同名键：托管块会覆盖它们，必须先让人确认。"""
    found: list[str] = []
    for line in outside:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in managed and key not in found:
            found.append(key)
    return found


def _write_env(profile: str) -> None:
    """只把「装配清单」写进 .env —— 它是全仓共享配置，测试与 CI 都会读到。"""
    env_path = ROOT / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    outside, _inside, _seen = _split_env(text)
    foreign = _foreign_keys(outside, {"ASSEMBLY_PROFILE"})
    if foreign:
        raise InstallerError(
            f".env 里已手写这些键：{', '.join(foreign)}；"
            "请先删除/注释它们再安装（托管块会覆盖同名键，安装器不替你做这个决定）"
        )
    while outside and not outside[-1].strip():
        outside.pop()
    block = _env_block(profile)
    lines = list(outside)
    if block:
        lines.append("")
        lines.extend(block)
    env_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def _write_runtime_env(env_items: dict[str, str]) -> None:
    """"运行开关"放独立文件：只由 scripts/run_backend.ps1 注入环境变量。

    刻意不写 `.env`：`A2A_ENABLED` 这类开关的默认值是测试与契约的一部分
    （`tests/integration/test_v13_a2a_api.py::test_disabled_gateway_is_503`
    依赖默认 false），塞进 `.env` 会让"装了插件"悄悄改掉别人机器上的默认行为。
    """
    if not env_items:
        if RUNTIME_ENV_PATH.exists():
            RUNTIME_ENV_PATH.unlink()
        return
    lines = [
        "# 由 scripts/zt_plugins.py 生成：插件运行开关（装配 ≠ 启用）",
        "# 仅由 scripts/run_backend.ps1 注入环境变量；卸载/重装会自动更新或删除本文件",
    ]
    lines.extend(f"{key}={value}" for key, value in sorted(env_items.items()))
    RUNTIME_ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _current_profile() -> str:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return BASE_PROFILE
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("ASSEMBLY_PROFILE="):
            return stripped.split("=", 1)[1].strip() or BASE_PROFILE
    return BASE_PROFILE


def _run(command: list[str], *, label: str) -> None:
    print(f"  $ {' '.join(command[1:])}")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        raise InstallerError(f"{label}失败：\n    " + "\n    ".join(tail))
    print(f"  ok {label}")


def _sync_runtime() -> None:
    py = sys.executable
    _run([py, "-m", "alembic", "upgrade", "head"], label="插件表结构已就绪")
    _run([py, "-m", "app.cli"], label="权限种子已补齐")


def _render_profile(installed_order: list[str], community: set[str]) -> str:
    extra = [plugin_id for plugin_id in installed_order if plugin_id not in community]
    return f"{BASE_PROFILE}+{','.join(extra)}" if extra else BASE_PROFILE


def cmd_list(_args: argparse.Namespace) -> None:
    index = _read_index()
    state = _installed_map(_read_state())
    print(f"\n社区版插件市场（{INDEX_PATH.relative_to(ROOT)}）\n")
    print(f"  {'ID':<24}{'版本':<10}{'状态':<12}说明")
    print("  " + "-" * 96)
    for package in index["packages"]:
        status = "已安装" if package["id"] in state else "未安装"
        if package["community_assembled"] and package["id"] not in state:
            status = "已内置"
        print(f"  {package['id']:<24}{package['version']:<10}{status:<12}{package['summary']}")
    print("\n  安装：python scripts/zt_plugins.py install <id> [<id> ...]")
    print("  卸载：python scripts/zt_plugins.py uninstall <id> | --all\n")


def cmd_status(_args: argparse.Namespace) -> None:
    index = {p["id"]: p for p in _read_index()["packages"]}
    state = _read_state()
    installed = _installed_map(state)
    community = _community_plugins()
    profile = _current_profile()
    print("\n当前装配档位：" + profile)
    print(f"社区档已装配 {len(community)} 个插件；市场包 {len(index)} 个")
    if RUNTIME_ENV_PATH.exists():
        print(f"运行开关文件：{RUNTIME_ENV_PATH.relative_to(ROOT)}（由 scripts/run_backend.ps1 注入）")
    print()
    print(f"  {'ID':<24}{'状态':<10}{'装配来源':<14}启用开关")
    print("  " + "-" * 96)
    for plugin_id, package in index.items():
        if plugin_id in installed:
            origin = "社区档内置" if plugin_id in community else "选配追加"
            status = "已安装"
        else:
            origin = "社区档内置" if plugin_id in community else "-"
            status = "-"
        switches = ", ".join(f"{k}={v}" for k, v in package["env"].items()) if status != "-" else "-"
        print(f"  {plugin_id:<24}{status:<10}{origin:<14}{switches}")
    skipped = [plugin_id for plugin_id in installed if plugin_id in community]
    if skipped:
        print(f"\n  注：{', '.join(skipped)} 属社区档内置，安装只开启执行开关，不写入装配清单。")
    print()


def cmd_install(args: argparse.Namespace) -> None:
    index = {p["id"]: p for p in _read_index()["packages"]}
    unknown = [plugin_id for plugin_id in args.ids if plugin_id not in index]
    if unknown:
        _die(f"市场里没有这些插件：{', '.join(unknown)}（先 python scripts/zt_plugins.py list）")

    state = _read_state()
    installed = _installed_map(state)
    community = _community_plugins()
    order: list[str] = [item["id"] for item in state.get("installed", [])]
    total = len(args.ids)

    print(f"\n准备安装 {total} 个插件\n")
    for step, plugin_id in enumerate(args.ids, start=1):
        package = index[plugin_id]
        prefix = f"[{step}/{total}]"
        if plugin_id in installed:
            print(f"{prefix} {plugin_id} 已安装（v{installed[plugin_id]['version']}），跳过")
            continue
        print(f"{prefix} {plugin_id} v{package['version']} — {package['display_name']}")
        archive = _download(package)
        print(f"      下载完成 {archive.relative_to(ROOT)}  sha256 {package['sha256'][:12]}…")
        target = _extract(package, archive)
        print(f"      解压完成 {target.relative_to(ROOT)}")
        installed[plugin_id] = {
            "id": plugin_id,
            "version": package["version"],
            "display_name": package["display_name"],
            "installed_at": _now(),
            "package_sha256": package["sha256"],
            "env": dict(package["env"]),
            "entry": package["entry"],
        }
        order.append(plugin_id)
        state["installed"] = [installed[item] for item in order]
        _write_state(state)
        if plugin_id in community:
            print("      社区档已内置装配，本次只启用其执行开关")
        else:
            print("      已加入装配清单（重启后生效）")

    env_items: dict[str, str] = {}
    for plugin_id in order:
        env_items.update(installed[plugin_id].get("env", {}))
    profile = _render_profile(order, community)

    print("\n[运行时] 同步插件表结构与权限种子")
    _sync_runtime()

    print("\n[配置] 写入装配清单与运行开关")
    _write_env(profile)
    _write_runtime_env(env_items)
    print(f"  .env          ASSEMBLY_PROFILE={profile}")
    for key, value in sorted(env_items.items()):
        print(f"  .env.plugins  {key}={value}")

    print("\n安装完成。下一步：\n")
    print("  1) 重启后端： powershell -ExecutionPolicy Bypass -File scripts\\run_backend.ps1")
    print("  2) 打开控制台： http://127.0.0.1:5174 （console 目录下 npm run dev）")
    print("  3) 审计页 →「导出与归档」；未登录/无权限行为见 README")
    print("\n  撤回： python scripts/zt_plugins.py uninstall --all\n")


def cmd_uninstall(args: argparse.Namespace) -> None:
    state = _read_state()
    installed = _installed_map(state)
    targets = list(installed) if args.all else list(args.ids)
    if not targets:
        _die("请指定插件 id，或使用 --all 全部卸载")
    missing = [plugin_id for plugin_id in targets if plugin_id not in installed]
    if missing and not args.all:
        _die(f"这些插件并未安装：{', '.join(missing)}")

    community = _community_plugins()
    remaining = [plugin_id for plugin_id in installed if plugin_id not in targets]
    print(f"\n卸载 {len(targets)} 个插件\n")
    for plugin_id in targets:
        record = installed[plugin_id]
        target = INSTALLED_DIR / plugin_id
        if target.exists():
            shutil.rmtree(target)
        archive = DOWNLOAD_DIR / f"{plugin_id}-{record['version']}.zip"
        if archive.exists():
            archive.unlink()
        print(f"  - {plugin_id} v{record['version']} 已移除")
    state["installed"] = [installed[plugin_id] for plugin_id in remaining]
    _write_state(state)

    env_items: dict[str, str] = {}
    for plugin_id in remaining:
        env_items.update(installed[plugin_id].get("env", {}))
    profile = _render_profile(remaining, community)
    _write_env(profile)
    _write_runtime_env(env_items)
    print(f"\n  .env 托管块已重建：ASSEMBLY_PROFILE={profile}")
    if profile == BASE_PROFILE:
        print("  托管块已整块删除，.env 回到安装前的状态")
    if not env_items:
        print("  .env.plugins 已删除，插件运行开关回到默认值")
    print("\n重启后端后生效。\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zt_plugins", description="Z-Torque 社区版插件市场安装器（离线包）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出市场插件与安装状态").set_defaults(func=cmd_list)
    sub.add_parser("status", help="查看当前装配档位与启用开关").set_defaults(func=cmd_status)

    install = sub.add_parser("install", help="安装并装配一个或多个插件")
    install.add_argument("ids", nargs="+", metavar="id")
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser("uninstall", help="卸载插件（恢复装配清单）")
    uninstall.add_argument("ids", nargs="*", metavar="id")
    uninstall.add_argument("--all", action="store_true", help="卸载全部已装插件")
    uninstall.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except InstallerError as exc:
        _die(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
