"""统一时间网格。

各信号各采各的、时间戳对不齐，对齐与按炉回放必须共用同一根时间轴：
:class:`TimeGrid` 以窗口起点为锚点（``origin``），按固定周期 ``period_s``
向右展开，窗口末端取闭区间。网格只负责"时刻怎么排"，不掺信号语义；信号
怎么落到网格上由 :mod:`coke.alignment.signals` 按信号类型决定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_EPS = 1e-9


@dataclass(frozen=True)
class TimeGrid:
    """一条从 ``origin`` 起、按固定周期展开的统一时间网格（末端闭区间）。"""

    origin: float
    period_s: float
    count: int

    @classmethod
    def build(cls, start_ts: float, end_ts: float, period_s: float) -> "TimeGrid":
        """按窗口与周期构造网格。

        末端取闭区间：恰好落在 ``end_ts`` 的网格点保留。窗口退化成单点时
        只有锚点一个点；``end_ts < start_ts`` 是调用方时序没守住，直接拒绝。
        """

        if period_s <= 0:
            raise ValueError("网格周期必须为正")
        if end_ts < start_ts - _EPS:
            raise ValueError("网格窗口结束时间不得早于开始时间")
        span = max(0.0, end_ts - start_ts)
        count = int(math.floor(span / period_s + _EPS)) + 1
        return cls(origin=float(start_ts), period_s=float(period_s), count=count)

    def ts_at(self, index: int) -> float:
        """返回第 ``index`` 个网格点的时间戳。"""

        return self.origin + index * self.period_s

    def index_of(self, ts: float) -> int:
        """返回时间戳对应的网格下标（按周期取整，不做夹取）。"""

        return int(math.floor((ts - self.origin) / self.period_s + _EPS))

    def clamp_index(self, ts: float) -> int:
        """把时间戳夹到网格下标范围 ``[0, count-1]``。"""

        return min(max(self.index_of(ts), 0), self.count - 1)

    def cover_indices(self, start_ts: float, end_ts: float) -> tuple[int, int]:
        """返回某闭区间在网格上覆盖的首尾下标（已夹取）。"""

        i0 = self.clamp_index(start_ts)
        i1 = self.clamp_index(end_ts)
        if i1 < i0:
            i1 = i0
        return i0, i1

    def timestamps(self) -> list[float]:
        """返回全部网格点时间戳。"""

        return [self.ts_at(index) for index in range(self.count)]

    def as_dict(self, *, full_precision: bool = False) -> dict:
        end = self.ts_at(self.count - 1)
        return {
            "origin": self.origin if full_precision else round(self.origin, 3),
            "period_s": self.period_s,
            "count": self.count,
            "end_ts": end if full_precision else round(end, 3),
        }
