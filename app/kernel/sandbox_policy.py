"""沙箱 policy gate：`(tool, sandbox_mode, agent) → allow/deny`，deny-wins（§2.3）。

纯内核逻辑，无 DB/IO 依赖；manifest 声明的 permissions 必须经本闸门消费，
不能只在 `permission_checker` 里查一遍然后被执行点忽略。任何一条规则命中 deny
即返回拒绝，执行点不得用"后续规则放行"覆盖。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.kernel.ports.sandbox_driver import (
    SANDBOX_ISOLATION_CONTAINER,
    SANDBOX_MODE_FULL,
    SandboxScope,
)

DENY_CAPABILITY = "AI_CAPABILITY_UNAVAILABLE"
DENY_SCOPE = "AI_SANDBOX_DENIED"
DENY_PERMISSION = "AI_PERMISSION_DENIED"


@dataclass(frozen=True)
class SandboxDecision:
    allowed: bool
    reason: str = ""
    code: str | None = None


def evaluate_sandbox_policy(
    *,
    tool: str,
    scope: SandboxScope,
    agent_id: str | None,
    manifest_permissions: Sequence[str],
    granted_permissions: Sequence[str],
    target_paths: Sequence[str | Path] = (),
    denied_agents: Sequence[str] = (),
) -> SandboxDecision:
    """deny-wins 判定：返回第一个命中拒绝的规则，全部通过才 allow。"""
    if agent_id and agent_id in denied_agents:
        return SandboxDecision(False, f"agent {agent_id} is denied by sandbox policy", DENY_SCOPE)
    missing = sorted(set(manifest_permissions) - set(granted_permissions))
    if missing:
        return SandboxDecision(False, f"manifest permissions not granted: {', '.join(missing)}", DENY_PERMISSION)
    if "sandbox.full" in manifest_permissions and scope.mode != SANDBOX_MODE_FULL:
        return SandboxDecision(False, "tool declares sandbox.full but scope is restricted", DENY_SCOPE)
    if scope.mode == SANDBOX_MODE_FULL and scope.isolation != SANDBOX_ISOLATION_CONTAINER:
        return SandboxDecision(False, "full scope requires container isolation; no silent process fallback", DENY_CAPABILITY)
    for item in target_paths:
        if not scope.permits(item):
            return SandboxDecision(False, f"path is outside sandbox scope: {tool}", DENY_SCOPE)
    return SandboxDecision(True)
