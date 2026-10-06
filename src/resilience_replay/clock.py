"""演练统一时钟：所有时间戳均为带时区的 ISO-8601，内部按 UTC 比较。"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(value: str | datetime) -> datetime:
    """把外部输入解析为 UTC datetime；拒绝无时区时间，避免跨单位歧义。"""
    if isinstance(value, datetime):
        dt = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("时间戳必须是非空 ISO-8601 字符串")
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳缺少时区信息: {value!r}")
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    """输出带 UTC 偏移的稳定字符串。"""
    return dt.astimezone(timezone.utc).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
