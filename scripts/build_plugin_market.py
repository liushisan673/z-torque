"""生成社区版「插件市场」离线包（模拟下载源）。

产物 `plugins_market/`：

    index.json                 市场索引（含每个包的 sha256 / 大小 / 展示信息）
    <id>-<version>.zip         插件包（plugin.json + README.md + env.fragment.json）

安装走 `scripts/zt_plugins.py`：下载 → 校验 sha256 → 解压落地 → 重建装配清单
→ 补权限种子。市场包只描述"这个插件是什么、装完要开什么开关"，
插件实现仍在 `app/builtin_plugins/`（社区版内置、按 Profile 决定装配）。

用法：python scripts/build_plugin_market.py
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MARKET_DIR = ROOT / "plugins_market"

_SANDBOX_README = """# sandbox_driver 1.3.0 — MD-Script 执行沙箱

## 作用
把 MD-Skill 里的脚本执行关进沙箱：独立进程 + POSIX rlimit（CPU / 文件大小 / 进程数）
+ 工作目录白名单。未装配时 MD-Script 返回 503 AI_CAPABILITY_UNAVAILABLE。

## 本包的特殊之处
社区版已经内置装配（`execution` 模块），所以「安装」只做**启用**：
`MD_SKILL_SCRIPT_ENABLED` 默认 false（脚本一律不执行），安装后置为 true。

## 启用后仍然受这些约束
- `SANDBOX_ALLOWED_ROOTS` 白名单（未配置 → `[MD_SKILLS_DIR]`；显式空数组 = 装配失败）
- `SANDBOX_MAX_PROCESSES` / `SANDBOX_MAX_FILE_BYTES`
- Windows 没有 rlimit 语义，capabilities 会如实上报 `rlimit=False`、`isolation_level=none`

## 验证
`GET /api/z-torque/capabilities` → `data.sandbox.available == true`
"""

_AUDIT_README = """# audit_export_archive 1.3.0 — 审计导出与归档

## 作用
按筛选范围把审计日志**脱敏导出**为 JSONL / CSV 分片（后台任务 + 前台轮询 + 分片下载），
并支持归档批次（可选加密、密钥轮换）。

## 装完还需要什么
- 权限：`audit.export`（导出）、`audit.archive`（归档）—— 安装器会跑一次权限种子，
  admin 角色自动获得（不装权限则前端入口不渲染，这是刻意的）
- 格式白名单：`AUDIT_EXPORT_FORMATS` 默认 `["jsonl"]`；本包安装时放开 `["jsonl","csv"]`

## 验证
前端：审计页 →「导出与归档」→ 导出当前筛选 → 轮询到 SUCCEEDED → 查看分片 → 下载
接口：`POST /api/z-torque/audit-exports`
"""

_A2A_README = """# a2a_gateway 1.3.0 — Agent-to-Agent 网关

## 作用
A2A 协议入站 / 出站：身份传播（`none` / `user_id_header` / `full_context_token`）、
调用深度与扇出限制、出站网络白名单。

## 装完还需要什么
- `A2A_ENABLED=true`（默认 false；关闭时端点仍注册，返回 503 `AI_A2A_DISABLED`）
- 权限 `a2a.invoke`（安装器已种入；普通成员角色默认不授予）
- 入站鉴权：未带凭据 → 403 `AI_A2A_DENIED`（这是闸门，不是故障）

## 验证
`POST /api/z-torque/a2a/invoke`
未授权 → 403 `AI_A2A_DENIED`；开关关闭 → 503 `AI_A2A_DISABLED`；
未装配 → 503 `AI_CAPABILITY_UNAVAILABLE`
"""

PACKAGES: tuple[dict[str, Any], ...] = (
    {
        "id": "sandbox_driver",
        "version": "1.3.0",
        "display_name": "MD-Script 执行沙箱",
        "summary": "脚本执行隔离：独立进程 + rlimit + 目录白名单（社区档已内置装配，安装即启用）",
        "entry": "app.builtin_plugins.sandbox_driver:SandboxDriverPlugin",
        "community_assembled": True,
        "permissions": [],
        "env": {"MD_SKILL_SCRIPT_ENABLED": "true"},
        "readme": _SANDBOX_README,
    },
    {
        "id": "audit_export_archive",
        "version": "1.3.0",
        "display_name": "审计导出与归档",
        "summary": "审计日志脱敏导出为 JSONL/CSV 分片，并支持加密归档批次",
        "entry": "app.builtin_plugins.audit_export_archive:AuditExportArchivePlugin",
        "community_assembled": False,
        "permissions": ["audit.export", "audit.archive"],
        "env": {"AUDIT_EXPORT_FORMATS": '["jsonl","csv"]'},
        "readme": _AUDIT_README,
    },
    {
        "id": "a2a_gateway",
        "version": "1.3.0",
        "display_name": "A2A 网关",
        "summary": "Agent-to-Agent 入站/出站协议网关，含身份传播与深度/扇出限制",
        "entry": "app.builtin_plugins.a2a_gateway:A2AGatewayPlugin",
        "community_assembled": False,
        "permissions": ["a2a.invoke"],
        "env": {"A2A_ENABLED": "true"},
        "readme": _A2A_README,
    },
)


def _plugin_json(package: dict[str, Any]) -> dict[str, Any]:
    """包内元数据：安装器与前端市场都只依赖这份声明。"""
    return {
        "schema": 1,
        "id": package["id"],
        "version": package["version"],
        "display_name": package["display_name"],
        "summary": package["summary"],
        "entry": package["entry"],
        "community_assembled": package["community_assembled"],
        "permissions": list(package["permissions"]),
        "env": dict(package["env"]),
    }


def _package_members(package: dict[str, Any]) -> dict[str, str]:
    return {
        "plugin.json": json.dumps(_plugin_json(package), ensure_ascii=False, indent=2) + "\n",
        "env.fragment.json": json.dumps(package["env"], ensure_ascii=False, indent=2) + "\n",
        "README.md": package["readme"],
    }


def build() -> Path:
    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {
        "schema": 1,
        "generator": "scripts/build_plugin_market.py",
        "packages": [],
    }
    for package in PACKAGES:
        zip_path = MARKET_DIR / f"{package['id']}-{package['version']}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in _package_members(package).items():
                archive.writestr(name, text.encode("utf-8"))
        payload = zip_path.read_bytes()
        index["packages"].append(
            {
                "id": package["id"],
                "version": package["version"],
                "display_name": package["display_name"],
                "summary": package["summary"],
                "entry": package["entry"],
                "community_assembled": package["community_assembled"],
                "permissions": list(package["permissions"]),
                "env": dict(package["env"]),
                "file": zip_path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
        print(f"  packed {zip_path.name} ({len(payload)} bytes)")
    index_path = MARKET_DIR / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  index  {index_path.relative_to(ROOT)}  ({len(index['packages'])} packages)")
    return index_path


if __name__ == "__main__":
    build()
