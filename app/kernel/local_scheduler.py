"""单机统一调度器（Phase 2 §四：自动化调度 / cron）。

进程内 interval + cron 定时任务，零新依赖、零外部服务；同时收口此前散落在
MdSkillWatcher / audit_export_archive 的 threading.Timer 与 asyncio 循环。

不变量：
- 单一入口：所有周期任务都在 lifespan 里 register + start，停止时统一 cancel 并确认退出；
- 失败隔离：单轮异常只记状态，不得让调度循环终止或影响其它任务；
- 可观测：status() 暴露 runs / failures / last_run_at / last_error（不暴露凭据）；
- 可手动触发：trigger(name) 供测试与运维复现，不等下一个周期。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.core.errors import ApiError

logger = logging.getLogger("z_torque.scheduler")

Callback = Callable[[], Awaitable[Any] | Any]
MAX_CRON_SEARCH_MINUTES = 60 * 24 * 366


def _parse_field(expr: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        if not part:
            raise ApiError("AI_SCHEDULE_INVALID", "empty cron field", 400)
        if part == "*":
            values.update(range(low, high + 1))
        elif "/" in part:
            base, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ApiError("AI_SCHEDULE_INVALID", f"invalid cron step: {part}", 400) from exc
            if step <= 0:
                raise ApiError("AI_SCHEDULE_INVALID", f"invalid cron step: {part}", 400)
            start = low if base == "*" else int(base)
            values.update(range(start, high + 1, step))
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            values.update(range(int(start_text), int(end_text) + 1))
        else:
            values.add(int(part))
    if any(value < low or value > high for value in values):
        raise ApiError("AI_SCHEDULE_INVALID", f"cron field out of range: {expr}", 400)
    return values


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """解析标准 5 字段 cron：分 时 日 月 周（周 0=周日）。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ApiError("AI_SCHEDULE_INVALID", "cron must have 5 fields", 400)
    try:
        return (
            _parse_field(parts[0], 0, 59),
            _parse_field(parts[1], 0, 23),
            _parse_field(parts[2], 1, 31),
            _parse_field(parts[3], 1, 12),
            _parse_field(parts[4], 0, 6),
        )
    except ValueError as exc:
        raise ApiError("AI_SCHEDULE_INVALID", f"invalid cron expression: {expr}", 400) from exc


def cron_matches(expr: str, moment: datetime) -> bool:
    minute, hour, day, month, weekday = parse_cron(expr)
    # Python weekday: Mon=0..Sun=6；cron weekday: Sun=0..Sat=6
    cron_weekday = (moment.weekday() + 1) % 7
    return (
        moment.minute in minute
        and moment.hour in hour
        and moment.day in day
        and moment.month in month
        and cron_weekday in weekday
    )


def next_cron_delay(expr: str, now: datetime | None = None) -> float:
    """距离下一次 cron 匹配的秒数（分钟粒度，最多向前搜索一年）。"""
    current = now or datetime.now(timezone.utc)
    candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(MAX_CRON_SEARCH_MINUTES):
        if cron_matches(expr, candidate):
            return (candidate - current).total_seconds()
        candidate += timedelta(minutes=1)
    raise ApiError("AI_SCHEDULE_INVALID", "cron expression has no occurrence within a year", 400)


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    callback: Callback
    interval_seconds: float | None = None
    cron: str | None = None
    enabled: bool = True
    run_immediately: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if (self.interval_seconds is None) == (self.cron is None):
            raise ApiError("AI_SCHEDULE_INVALID", "exactly one of interval_seconds or cron is required", 400)
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ApiError("AI_SCHEDULE_INVALID", "interval_seconds must be positive", 400)
        if self.cron is not None:
            parse_cron(self.cron)


class LocalScheduler:
    """进程内调度器：由 lifespan 统一 start/stop，不自行注册 signal 或 atexit。"""

    def __init__(self) -> None:
        self._specs: dict[str, ScheduleSpec] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stats: dict[str, dict[str, Any]] = {}

    def register(self, spec: ScheduleSpec) -> None:
        if spec.name in self._specs:
            raise ApiError("AI_SCHEDULE_DUPLICATE", f"schedule already registered: {spec.name}", 409)
        self._specs[spec.name] = spec
        self._stats[spec.name] = {
            "runs": 0, "failures": 0, "last_run_at": None, "last_error": None,
        }

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)
        self._stats.pop(name, None)
        task = self._tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "enabled": spec.enabled,
                "interval_seconds": spec.interval_seconds,
                "cron": spec.cron,
                "running": name in self._tasks and not self._tasks[name].done(),
                **self._stats[name],
            }
            for name, spec in self._specs.items()
        ]

    async def start(self) -> None:
        for name, spec in self._specs.items():
            if spec.enabled and name not in self._tasks:
                self._tasks[name] = asyncio.create_task(self._loop(spec), name=f"schedule:{name}")

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def trigger(self, name: str) -> Any:
        spec = self._specs.get(name)
        if spec is None:
            raise ApiError("AI_SCHEDULE_NOT_FOUND", f"schedule not found: {name}", 404)
        return await self._execute(spec)

    async def _execute(self, spec: ScheduleSpec) -> Any:
        stats = self._stats[spec.name]
        try:
            if inspect.iscoroutinefunction(spec.callback):
                result = await spec.callback()
            else:
                # 同步回调可能在做 IO（如清理过期对象），放线程避免阻塞事件循环
                result = await asyncio.to_thread(spec.callback)
            stats["runs"] += 1
            stats["last_run_at"] = datetime.now(timezone.utc).isoformat()
            stats["last_error"] = None
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单轮失败不得终止调度循环
            stats["failures"] += 1
            stats["last_error"] = str(exc)[:200]
            logger.warning("scheduled job %s failed", spec.name, exc_info=True)
            return None

    async def _loop(self, spec: ScheduleSpec) -> None:
        try:
            if spec.run_immediately:
                await self._execute(spec)
            while True:
                delay = (
                    spec.interval_seconds
                    if spec.interval_seconds is not None
                    else next_cron_delay(spec.cron or "0 0 * * *")
                )
                await asyncio.sleep(delay)
                await self._execute(spec)
        except asyncio.CancelledError:
            raise
