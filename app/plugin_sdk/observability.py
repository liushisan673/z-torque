"""可观测性写入缝（observability.sink 的插件侧调用入口）。

插件（如 sandbox_driver）需要上报执行元信息，但不得 import 内核注册表实现。
此处刻意**每次调用惰性解析**内核实现，而不是 `from ... import emit_event`，
这样测试里 monkeypatch 内核实现仍然生效（替身与真实路径共用同一函数对象）。
"""

from __future__ import annotations

from typing import Any


def emit_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """向 observability.sink 上报一个事件（未配置 sink 时由内核决定丢弃）。"""
    from app.kernel.registry.active import emit_event as _emit

    _emit(event, payload)


__all__ = ["emit_event"]
