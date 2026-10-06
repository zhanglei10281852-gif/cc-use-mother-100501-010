"""时间解析与半开区间 [start, end) 的集合运算。

失效与绕行一律使用半开区间：右端点表示恢复动作的生效时刻，
因此 ``[08:00, 09:20)`` 与 ``[09:20, ...)`` 首尾相接而不重叠。
``end is None`` 表示截至重放评估时刻仍然开放的区间。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

_MIN = datetime.min.replace(tzinfo=timezone.utc)


def parse_ts(value: str) -> datetime:
    """把 ISO8601 文本统一为 UTC aware datetime，接受 ``Z`` 后缀。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间戳不能为空")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间戳: {value!r}") from exc
    if moment.tzinfo is None:
        # 演练时间戳必须显式带时区；裸时间按 UTC 处理以保证可比较。
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    """以统一的 UTC ``Z`` 文本输出计算结果中的时间点。"""
    moment = moment.astimezone(timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class Interval:
    """半开区间，``end=None`` 表示右端开放。"""

    start: datetime
    end: datetime | None

    def __iter__(self):
        return iter((self.start, self.end))


def _end_key(end: datetime | None) -> datetime:
    return _MIN if end is not None else datetime.max.replace(tzinfo=timezone.utc)


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """求并集；半开相邻区间（首尾相接）也合并。"""
    ordered = sorted(intervals, key=lambda item: (item.start, _end_key(item.end)))
    merged: list[list[datetime | None]] = []
    for item in ordered:
        if not merged:
            merged.append([item.start, item.end])
            continue
        last_start, last_end = merged[-1]
        if last_end is None:
            continue  # 开放区间吞没一切后续
        if item.start <= last_end:
            if item.end is None or item.end > last_end:
                merged[-1] = [last_start, item.end]
        else:
            merged.append([item.start, item.end])
    return [Interval(start, end) for start, end in merged]


def subtract_intervals(target: Interval, cuts: list[Interval]) -> list[Interval]:
    """``target - union(cuts)``，保留半开语义，正确处理开放端。"""
    parts: list[list[datetime | None]] = [[target.start, target.end]]
    for cut in merge_intervals(list(cuts)):
        remaining: list[list[datetime | None]] = []
        cut_start, cut_end = cut.start, cut.end
        for start, end in parts:
            # 切口整体在本段之前或之后。
            if cut_end is not None and cut_end <= start:
                remaining.append([start, end])
                continue
            if end is not None and cut_start >= end:
                remaining.append([start, end])
                continue
            # 存在重叠：保留左侧残余。
            if cut_start > start:
                remaining.append([start, cut_start])
            # 右侧残余；切口右端开放时吞没至 +∞。
            if cut_end is not None and (end is None or cut_end < end):
                remaining.append([cut_end, end])
        parts = remaining
    return [Interval(start, end) for start, end in parts if end is None or start < end]


def intersect_intervals(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """求交集；``A ∩ B = A − (A − B)``，复用半开减法。"""
    if not left or not right:
        return []
    result: list[Interval] = []
    for item in left:
        residual = subtract_intervals(item, right)
        result.extend(subtract_intervals(item, residual))
    return merge_intervals(result)


def total_seconds(intervals: list[Interval]) -> float | None:
    """区间累计秒数；存在开放区间时返回 ``None``。"""
    total = 0.0
    for item in intervals:
        if item.end is None:
            return None
        total += (item.end - item.start).total_seconds()
    return total
