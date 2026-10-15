"""统一时间序列化：全站 API 输出的时间戳必须带 UTC 时区后缀。

背景：数据库列均为 naive UTC（SQLite 无时区），直接 isoformat() 输出
无后缀字符串，前端 new Date() 会误判为本地时间，导致显示时间少 8 小时
（UTC+8 环境）。本模块提供两处统一入口：

- iso_utc(dt): 手工 dict 序列化用
- UTCDatetime: pydantic schema 字段类型，model_validate 时把 naive 标注为 UTC
"""
from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _ensure_utc(dt).isoformat()


# pydantic v2 字段类型：naive 视为 UTC；序列化输出带 +00:00 后缀
UTCDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
