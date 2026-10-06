"""半开区间运算与时间解析的边界测试。"""

from __future__ import annotations

import unittest

from resilience_replay.time_model import (
    Interval,
    intersect_intervals,
    merge_intervals,
    parse_ts,
    subtract_intervals,
)


def I(start: str, end: str | None):
    day = "2026-09-30T"
    return Interval(
        parse_ts(day + start if not start.startswith("20") else start),
        parse_ts(day + end if (end and not end.startswith("20")) else None) if end else None,
    )


class IntervalTests(unittest.TestCase):
    def test_adjacent_half_open_intervals_merge(self) -> None:
        merged = merge_intervals([I("08:00:00Z", "09:00:00Z"),
                                  I("09:00:00Z", "10:00:00Z")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0], I("08:00:00Z", "10:00:00Z"))

    def test_subtract_middle_hole(self) -> None:
        result = subtract_intervals(
            I("08:00:00Z", "10:00:00Z"),
            [I("08:30:00Z", "09:00:00Z")],
        )
        self.assertEqual(result, [
            I("08:00:00Z", "08:30:00Z"),
            I("09:00:00Z", "10:00:00Z"),
        ])

    def test_subtract_open_end_cut(self) -> None:
        result = subtract_intervals(
            I("08:00:00Z", None),
            [I("09:00:00Z", "09:30:00Z")],
        )
        self.assertEqual(result[0], I("08:00:00Z", "09:00:00Z"))
        self.assertEqual(result[1], I("09:30:00Z", None))

    def test_intersection(self) -> None:
        result = intersect_intervals(
            [I("08:00:00Z", "09:00:00Z"), I("10:00:00Z", "11:00:00Z")],
            [I("08:30:00Z", "10:30:00Z")],
        )
        self.assertEqual(result, [
            I("08:30:00Z", "09:00:00Z"),
            I("10:00:00Z", "10:30:00Z"),
        ])

    def test_timezone_normalized_to_utc_z(self) -> None:
        self.assertEqual(
            parse_ts("2026-09-30T16:00:00+08:00"),
            parse_ts("2026-09-30T08:00:00Z"),
        )

    def test_touching_endpoint_does_not_overlap(self) -> None:
        # [08:00,09:00) 与 [09:00,10:00) 不相交（半开）。
        self.assertEqual(
            intersect_intervals([I("08:00:00Z", "09:00:00Z")],
                                [I("09:00:00Z", "10:00:00Z")]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
