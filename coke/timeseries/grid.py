"""统一时间网格、信号策略与对齐结果。"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

LINEAR = "linear"
PREVIOUS_HOLD = "previous_hold"
NEAREST_PREV = "nearest_prev"

OBSERVED = "observed"
INTERPOLATED_LINEAR = "interpolated_linear"
HELD_PREVIOUS = "held_previous"
NEAREST_PREVIOUS = "nearest_previous"
BAD_MISSING = "bad_missing"
BAD_STALE = "bad_stale"
BAD_CLOCK_ROLLBACK = "bad_clock_rollback"
BAD_EXTRAPOLATION = "bad_extrapolation"
BAD_BOUNDARY = "bad_boundary"

BAD_QUALITIES = {BAD_MISSING, BAD_STALE, BAD_CLOCK_ROLLBACK, BAD_EXTRAPOLATION, BAD_BOUNDARY}


@dataclass(frozen=True)
class SignalPolicy:
    """一类信号在统一时间网格上的取值规则。"""

    method: str
    max_gap_s: float
    max_age_s: float | None = None
    tolerance_s: float = 0.001
    dense: bool = True

    @classmethod
    def analog(cls, max_gap_s: float = 10.0, tolerance_s: float = 0.001) -> "SignalPolicy":
        """连续模拟量：包围点线性插值，不跨缺口或炉界外推。"""

        return cls(method=LINEAR, max_gap_s=max_gap_s, tolerance_s=tolerance_s)

    @classmethod
    def state(cls, max_age_s: float = 15.0, tolerance_s: float = 0.001) -> "SignalPolicy":
        """状态、开关、设定值：分段保持最近前值。"""

        return cls(method=PREVIOUS_HOLD, max_gap_s=math.inf, max_age_s=max_age_s, tolerance_s=tolerance_s)

    @classmethod
    def event(cls, tolerance_s: float = 0.001) -> "SignalPolicy":
        """事件与脉冲：只贴到最近网格点，不补点。"""

        return cls(method=NEAREST_PREV, max_gap_s=0.0, tolerance_s=tolerance_s, dense=False)

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "max_gap_s": None if math.isinf(self.max_gap_s) else self.max_gap_s,
            "max_age_s": self.max_age_s,
            "tolerance_s": self.tolerance_s,
            "dense": self.dense,
        }


@dataclass(frozen=True)
class TimeGrid:
    """以 Unix epoch 为零点的固定周期时间网格。"""

    period_s: float
    origin_s: float = 0.0

    def __post_init__(self) -> None:
        if self.period_s <= 0:
            raise ValueError("网格周期必须为正")

    def index_at(self, ts: float) -> int:
        """返回覆盖某时刻的网格序号。"""

        return int(math.floor((ts - self.origin_s) / self.period_s + 1e-9))

    def ts_at(self, index: int) -> float:
        """返回网格序号对应的时间戳。"""

        return self.origin_s + index * self.period_s

    def points(self, start_s: float, end_s: float) -> list[float]:
        """返回半开区间 ``[start_s, end_s)`` 内的全部网格点。"""

        if end_s < start_s:
            raise ValueError("时间区间结束值不能早于开始值")
        first = self.index_at(start_s)
        if self.ts_at(first) < start_s - 1e-9:
            first += 1
        count = max(0, int(math.ceil((end_s - self.ts_at(first)) / self.period_s - 1e-9)))
        return [self.ts_at(first + offset) for offset in range(count)]


@dataclass(frozen=True)
class ClockIssue:
    """一次采样时间戳倒退。"""

    signal: str
    rollback_ts: float
    previous_ts: float
    high_water_ts: float
    accepted_ts: float | None

    @property
    def start_ts(self) -> float:
        return min(self.rollback_ts, self.high_water_ts)

    @property
    def end_ts(self) -> float:
        return max(self.rollback_ts, self.high_water_ts)

    def affects(self, ts: float) -> bool:
        """网格点是否落在不可信的倒退影响区间。"""

        return self.start_ts - 1e-9 <= ts < self.end_ts - 1e-9

    def overlaps(self, start_s: float, end_s: float) -> bool:
        """倒退区间是否与某炉段相交。"""

        return self.start_ts < end_s and self.end_ts > start_s

    def as_dict(self) -> dict:
        return {
            "signal": self.signal,
            "rollback_ts": round(self.rollback_ts, 3),
            "previous_ts": round(self.previous_ts, 3),
            "high_water_ts": round(self.high_water_ts, 3),
            "accepted_ts": None if self.accepted_ts is None else round(self.accepted_ts, 3),
            "bad_start_ts": round(self.start_ts, 3),
            "bad_end_ts": round(self.end_ts, 3),
        }


@dataclass(frozen=True)
class AlignedPoint:
    """统一网格上的一个点。"""

    ts: float
    value: float | None
    quality: str
    source_ts: float | None = None

    @property
    def trusted(self) -> bool:
        return self.quality not in BAD_QUALITIES

    def as_dict(self) -> dict:
        return {
            "ts": round(self.ts, 3),
            "value": None if self.value is None else round(self.value, 4),
            "quality": self.quality,
            "trusted": self.trusted,
            "source_ts": None if self.source_ts is None else round(self.source_ts, 3),
        }


def _nearest_observed_at(observed: list[tuple[float, float]], ts: float, tolerance_s: float) -> tuple[float, float] | None:
    timestamps = [item[0] for item in observed]
    pos = bisect.bisect_left(timestamps, ts)
    candidates: list[tuple[float, float]] = []
    if pos < len(observed):
        candidates.append(observed[pos])
    if pos > 0:
        candidates.append(observed[pos - 1])
    if not candidates:
        return None
    source_ts, value = min(candidates, key=lambda item: abs(item[0] - ts))
    if abs(source_ts - ts) > tolerance_s:
        return None
    return source_ts, value


def _previous_observed(observed: list[tuple[float, float]], ts: float) -> tuple[float, float] | None:
    timestamps = [item[0] for item in observed]
    pos = bisect.bisect_right(timestamps, ts + 1e-9)
    if pos == 0:
        return None
    return observed[pos - 1]


def align_signal(
    signal: str,
    samples: list[tuple[float, float]],
    grid: TimeGrid,
    start_s: float,
    end_s: float,
    policy: SignalPolicy,
    clock_issues: list[ClockIssue] | None = None,
) -> list[AlignedPoint]:
    """把一个信号对齐到指定炉段内的统一网格。

    函数只使用炉段半开区间内的可信样本，因此任何插值和保持都不会跨过炉界。
    时间戳倒退样本由调用方隔离；这里仍用倒退区间给网格点打不可信标记。
    """

    issues = [issue for issue in (clock_issues or []) if issue.signal == signal]
    observed = sorted(
        (float(ts), float(value))
        for ts, value in samples
        if start_s <= ts < end_s
    )
    if policy.method == NEAREST_PREV and not policy.dense:
        snapped: dict[float, AlignedPoint] = {}
        for source_ts, value in observed:
            target_ts = grid.ts_at(grid.index_at(source_ts))
            if target_ts >= start_s and target_ts < end_s and source_ts - target_ts <= grid.period_s:
                snapped[target_ts] = AlignedPoint(target_ts, value, OBSERVED, source_ts)
        return [snapped[ts] for ts in sorted(snapped)]

    points: list[AlignedPoint] = []
    for ts in grid.points(start_s, end_s):
        if any(issue.affects(ts) for issue in issues):
            if policy.method == NEAREST_PREV and not policy.dense:
                continue
            points.append(AlignedPoint(ts, None, BAD_CLOCK_ROLLBACK))
            continue

        if policy.method == NEAREST_PREV:
            matched = _nearest_observed_at(observed, ts, policy.tolerance_s)
            if matched is None:
                if policy.dense:
                    points.append(AlignedPoint(ts, None, BAD_MISSING))
            else:
                source_ts, value = matched
                points.append(AlignedPoint(ts, value, OBSERVED, source_ts))
            continue

        previous = _previous_observed(observed, ts)
        if previous is None:
            points.append(AlignedPoint(ts, None, BAD_BOUNDARY if ts <= start_s + policy.tolerance_s else BAD_EXTRAPOLATION))
            continue

        previous_ts, previous_value = previous
        if abs(previous_ts - ts) <= policy.tolerance_s:
            points.append(AlignedPoint(ts, previous_value, OBSERVED, previous_ts))
            continue

        if policy.method == PREVIOUS_HOLD:
            if policy.max_age_s is not None and ts - previous_ts > policy.max_age_s:
                points.append(AlignedPoint(ts, None, BAD_STALE, previous_ts))
            else:
                points.append(AlignedPoint(ts, previous_value, HELD_PREVIOUS, previous_ts))
            continue

        timestamps = [item[0] for item in observed]
        next_pos = bisect.bisect_right(timestamps, ts + 1e-9)
        if next_pos >= len(observed):
            points.append(AlignedPoint(ts, None, BAD_EXTRAPOLATION, previous_ts))
            continue

        next_ts, next_value = observed[next_pos]
        gap = next_ts - previous_ts
        if gap > policy.max_gap_s + 1e-9:
            points.append(AlignedPoint(ts, None, BAD_MISSING, previous_ts))
            continue
        ratio = (ts - previous_ts) / gap
        value = previous_value + (next_value - previous_value) * ratio
        points.append(AlignedPoint(ts, value, INTERPOLATED_LINEAR, previous_ts))
    return points
