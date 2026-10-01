"""信号对齐：最近点前插（模拟量）／分段保持（状态量）与部分失败标记。"""

from __future__ import annotations

import pytest

from coke.alignment.grid import TimeGrid
from coke.alignment.signals import (
    ANALOG,
    STATE,
    REGRESSION,
    SAMPLE_GAP,
    RawSample,
    align_signal,
    policy_for_kind,
)


@pytest.fixture()
def grid() -> TimeGrid:
    return TimeGrid.build(0.0, 20.0, 5.0)


def test_analog_uses_last_hold_and_marks_first_sample_gap(grid):
    # 模拟量 0->5->10 三个真点，10 之后到 20 缺口 10s，超过 1 个周期。
    alignment = align_signal(
        "temperature",
        ANALOG,
        [RawSample(0.0, 100.0), RawSample(5.0, 101.0), RawSample(10.0, 102.0)],
        grid,
        max_gap_periods=1,
    )
    assert alignment.policy == "last_hold"
    values = [(point.ts, point.value) for point in alignment.points]
    assert values == [(0.0, 100.0), (5.0, 101.0), (10.0, 102.0), (15.0, 102.0), (20.0, 102.0)]
    assert [point.trusted for point in alignment.points] == [True, True, True, False, False]
    assert all(SAMPLE_GAP in point.reasons for point in alignment.points[3:])
    # 缺口两端的真样本本身保持可信。
    assert SAMPLE_GAP not in alignment.points[2].reasons


def test_state_uses_piecewise_hold_and_never_marks_gaps(grid):
    alignment = align_signal(
        "valve", STATE, [RawSample(0.0, 1.0), RawSample(20.0, 0.0)], grid, max_gap_periods=1
    )
    assert alignment.policy == "piecewise_hold"
    assert [point.value for point in alignment.points] == [1.0, 1.0, 1.0, 1.0, 0.0]
    assert all(point.trusted for point in alignment.points)
    assert all(not point.reasons for point in alignment.points)


def test_before_first_sample_is_uncovered_not_untrusted(grid):
    alignment = align_signal("late", ANALOG, [RawSample(10.0, 1.0), RawSample(15.0, 2.0)], grid)
    assert alignment.points[0].covered is False
    assert alignment.points[0].value is None
    assert alignment.points[0].trusted is True
    assert alignment.uncovered_points == 2


def test_timestamp_regression_is_partial_failure(grid):
    # 到达序：0,5,10 正常，随后一条 ts=3 的倒退重发，最后 ts=20 恢复。
    raw = [
        RawSample(0.0, 10.0, seq=1),
        RawSample(5.0, 11.0, seq=2),
        RawSample(10.0, 12.0, seq=3),
        RawSample(3.0, 99.0, seq=4),
        RawSample(20.0, 13.0, seq=5),
    ]
    alignment = align_signal("pressure", ANALOG, raw, grid, max_gap_periods=3)
    # 倒退样本被剔除，取值不受 99 影响。
    assert [point.value for point in alignment.points] == [10.0, 11.0, 12.0, 12.0, 13.0]
    assert alignment.points[0].trusted is True
    assert REGRESSION in alignment.points[1].reasons
    assert alignment.points[2].trusted is True
    assert len(alignment.regressions) == 1
    assert alignment.regressions[0]["ts"] == 3.0


def test_unknown_signal_kind_rejected():
    with pytest.raises(ValueError):
        policy_for_kind("gauge")


def test_aligned_point_rounding_modes(grid):
    alignment = align_signal("x", ANALOG, [RawSample(0.123456, 1.0)], grid)
    point = alignment.points[1]  # t=5，前插自 0.123456
    rounded = point.as_dict()
    precise = point.as_dict(full_precision=True)
    assert rounded["ts"] == 5.0
    assert rounded["source_ts"] == 0.123
    assert precise["source_ts"] == 0.123456
