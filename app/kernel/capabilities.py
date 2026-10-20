"""capabilities 阶段推导（P2-5：stage 由装配状态推导，不再硬编码）。

阶段语义沿用开发里程碑：每档 stage 以"定义性 Port 全部 RESOLVED"为门槛
（阶段00 §3.5 / 各阶段开发文档）。推导取满足门槛的最高档；必需 Port 缺失
（企业版裁剪）时 stage 自然回退，能力查询不虚报。
"""

from typing import Any

from app.kernel.registry.port_registry import PortState

# (stage, 该阶段定义性 Port, 说明)——按升序排列；里程碑阶段见 docs/ 各阶段文档
STAGE_GATES: tuple[tuple[int, tuple[str, ...], str], ...] = (
    (3, ("model.provider", "skill.executor", "queue.driver"), "对话与任务执行"),
    (7, ("connector",), "外部系统连接器"),
    (9, ("memory.short", "object.store"), "会话记忆与文件"),
    (11, ("secret.provider", "model.selector", "lock.provider"), "任务恢复/取消、委派与模型选择"),
)

# 门槛全不满足时的基线（工程骨架 + 认证，阶段01）
BASELINE_STAGE = "1"


def derive_stage(registry: Any | None) -> str:
    """由 Port 注册表解析状态推导当前 stage（字符串，与历史 capabilities 形状一致）。"""
    stage = BASELINE_STAGE
    if registry is None:
        return stage
    for number, ports, _description in STAGE_GATES:
        resolutions = [registry.resolution(name) for name in ports]
        if all(item is not None and item.state == PortState.RESOLVED for item in resolutions):
            stage = str(number)
    return stage
