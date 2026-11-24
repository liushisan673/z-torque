"""sandbox.driver Port 契约：显式 Scope + 唯一执行入口 + 事务式补丁（Phase 0 §2.3）。

Scope 是**不可变显式参数**：不进 ContextVar、不靠子进程继承；调用方必须逐层显式传递。
`execute(scope, ...)` 是唯一执行入口：先做 scope/隔离形态/路径校验，再委派实现 `run`。
`run` 保留为实现细节（由 `execute` 调用），任何绕过 `execute` 的直调都属违规（CI 静态拦截）。

`apply_patch` 是同一 Port 上的窄扩展（不新增第 22 个 Port）：默认不可用，由内置
sandbox_driver 插件实现；内核只持有契约与审计/落库，不实现文件写入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import ApiError

SANDBOX_MODE_RESTRICTED = "restricted"
SANDBOX_MODE_FULL = "full"
SANDBOX_MODES = (SANDBOX_MODE_RESTRICTED, SANDBOX_MODE_FULL)

SANDBOX_ISOLATION_PROCESS = "process"
SANDBOX_ISOLATION_CONTAINER = "container"
SANDBOX_ISOLATIONS = (SANDBOX_ISOLATION_PROCESS, SANDBOX_ISOLATION_CONTAINER)

PATCH_COMMITTED = "COMMITTED"
PATCH_ROLLED_BACK = "ROLLED_BACK"
PATCH_PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class SandboxScope:
    """不可变沙箱边界：路径白名单 + 权限模式 + 隔离形态。

    - restricted：脚本仅可见技能自身目录（可信开发态），process 隔离即可；
    - full：脚本可触及整个白名单根（工作区级写入），必须 container 隔离；
      单机 exe 无容器时显式 `AI_CAPABILITY_UNAVAILABLE`，绝不静默降级为 process。
    """

    allowed_roots: tuple[str, ...]
    mode: str = SANDBOX_MODE_RESTRICTED
    isolation: str = SANDBOX_ISOLATION_PROCESS

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_roots", tuple(str(item) for item in self.allowed_roots))
        if self.mode not in SANDBOX_MODES:
            raise ApiError("AI_SANDBOX_DENIED", f"invalid sandbox scope mode: {self.mode!r}", 403)
        if self.isolation not in SANDBOX_ISOLATIONS:
            raise ApiError("AI_SANDBOX_DENIED", f"invalid sandbox isolation: {self.isolation!r}", 403)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed_roots": list(self.allowed_roots), "mode": self.mode, "isolation": self.isolation}

    @classmethod
    def from_dict(cls, value: Any) -> SandboxScope:
        """从 checkpoint/DB 恢复 scope；结构不合法即拒绝（审批后篡改的第一道拦截）。"""
        if not isinstance(value, dict):
            raise ApiError("AI_SANDBOX_DENIED", "invalid sandbox scope in pending call", 403)
        roots = value.get("allowed_roots")
        if not isinstance(roots, list) or not roots or not all(isinstance(item, str) and item for item in roots):
            raise ApiError("AI_SANDBOX_DENIED", "sandbox scope roots are invalid", 403)
        return cls(tuple(roots), str(value.get("mode", "")), str(value.get("isolation", "")))

    def permits(self, path: str | Path) -> bool:
        resolved = Path(path).expanduser().resolve()
        return any(
            resolved == root or root in resolved.parents
            for root in (Path(item).expanduser().resolve() for item in self.allowed_roots)
        )


@dataclass(frozen=True)
class PatchSpec:
    """单文件补丁：目标路径、唯一匹配原文、替换文本、提交前复校验 hash。"""

    path: str
    old_text: str
    new_text: str
    expected_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "old_text": self.old_text, "new_text": self.new_text, "expected_hash": self.expected_hash}

    @classmethod
    def from_dict(cls, value: Any) -> PatchSpec:
        if not isinstance(value, dict):
            raise ApiError("AI_PATCH_INVALID", "patch must be an object", 422)
        path, old_text, new_text, expected_hash = (value.get("path"), value.get("old_text"), value.get("new_text"), value.get("expected_hash"))
        if not all(isinstance(item, str) and item for item in (path, expected_hash)) or not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ApiError("AI_PATCH_INVALID", "patch fields are invalid", 422)
        if not old_text:
            raise ApiError("AI_PATCH_INVALID", "old_text must not be empty", 422)
        return cls(path=path, old_text=old_text, new_text=new_text, expected_hash=expected_hash)


@dataclass(frozen=True)
class PatchFileResult:
    path: str
    before_hash: str
    after_hash: str
    before_text: str
    after_text: str


@dataclass
class PatchReport:
    status: str
    files: list[PatchFileResult] = field(default_factory=list)
    error_code: str | None = None
    message: str = ""


class SandboxDriver(ABC):
    """sandbox.driver Port 契约（21 Port 冻结，只允许窄扩展）。"""

    isolation: str = SANDBOX_ISOLATION_PROCESS

    @abstractmethod
    async def run(self, *, script_path: Path, arguments: dict[str, Any], cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict[str, Any]:
        """实现细节：由 execute() 调用，禁止外部直调。"""

    def declared_scope(self) -> SandboxScope:
        """驱动自身声明的边界（构造期白名单 + 隔离形态），由实现覆写。"""
        return SandboxScope(tuple(getattr(self, "allowed_roots", ()) or ()), isolation=self.isolation)

    async def execute(
        self,
        scope: SandboxScope,
        *,
        script_path: Path,
        arguments: dict[str, Any],
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """唯一执行入口：scope 校验 → 隔离形态匹配 → 委派实现。"""
        if scope.isolation != self.declared_scope().isolation:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", f"sandbox isolation {scope.isolation!r} is not available", 503)
        # full 模式触及工作区级写入，无 OS 隔离的 process 实现不得承接
        if scope.mode == SANDBOX_MODE_FULL and self.isolation != SANDBOX_ISOLATION_CONTAINER:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "full sandbox scope requires container isolation", 503)
        if not scope.permits(cwd) or not scope.permits(script_path):
            raise ApiError("AI_SANDBOX_DENIED", "sandbox scope does not cover script or working directory", 403)
        return await self.run(
            script_path=script_path, arguments=arguments, cwd=cwd,
            timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes,
        )

    async def apply_patch(self, scope: SandboxScope, *, patches: list[PatchSpec], base_dir: Path) -> PatchReport:
        """窄扩展：事务式补丁；未实现该能力的驱动必须显式报不可用。"""
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "patch capability is not available for this sandbox driver", 503)
