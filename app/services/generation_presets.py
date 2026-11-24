"""用户化生成预设：把「简洁准确/平衡/深度分析/创意发散」翻译成真实调用参数。

不新增内核 Port，也不新增依赖；只在 chat 入口把预设叠加到版本快照/全局默认之上，
模型网关真正收到 temperature / max_tokens 的变化。参数名不出现在前端。
"""

from __future__ import annotations

from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "concise": {"label": "简洁准确", "temperature": 0.2, "max_tokens_ratio": 0.7},
    "balanced": {"label": "平衡", "temperature": 0.7, "max_tokens_ratio": 1.0},
    "deep": {"label": "深度分析", "temperature": 0.4, "max_tokens_ratio": 1.5},
    "creative": {"label": "创意发散", "temperature": 1.0, "max_tokens_ratio": 1.2},
}


def apply_generation_preset(
    preset: str | None,
    temperature: float | None,
    max_tokens: int | None,
) -> tuple[float | None, int | None]:
    if not preset:
        return temperature, max_tokens
    config = PRESETS.get(preset)
    if config is None:
        return temperature, max_tokens
    new_max = max_tokens
    if max_tokens is not None:
        new_max = max(256, min(int(max_tokens * config["max_tokens_ratio"]), 32_000))
    return config["temperature"], new_max
