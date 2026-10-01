"""趋势缓冲、采样异常与统一网格对齐。

现场各信号原本各采各的，时间戳既不同步也可能倒退。缓冲只按接收顺序保存
原始点；时间戳倒退属于该信号的部分失败，倒退点被隔离到 ``clock_issues``，
不允许参与后续插值。真正的对齐策略由 :class:`~coke.timeseries.grid.SignalPolicy`
按信号类型决定。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .grid import ClockIssue, SignalPolicy, TimeGrid, align_signal


@dataclass(frozen=True)
class Sample:
    """一个带时间戳的采样点。"""

    ts: float
    value: float

    def as_dict(self) -> dict:
        return {"ts": round(self.ts, 3), "value": round(self.value, 4)}


class TrendBuffer:
    """按信号名保存原始趋势样本、策略和时间戳倒退记录。"""

    def __init__(self, limit: int = 2048, period_s: float = 5.0) -> None:
        self._series: dict[str, list[Sample]] = {}
        self._policies: dict[str, SignalPolicy] = {}
        self._issues: dict[str, list[ClockIssue]] = {}
        self._last_ts: dict[str, float] = {}
        self._high_water: dict[str, float] = {}
        self._limit = max(1, limit)
        self._period_s = max(0.001, period_s)
        self._lock = threading.RLock()
        self.register("collector_pressure", SignalPolicy.analog(max_gap_s=period_s * 2))
        self.register("damper_angle", SignalPolicy.state(max_age_s=period_s * 3))

    @property
    def period_s(self) -> float:
        return self._period_s

    def register(self, signal: str, policy: SignalPolicy) -> None:
        """登记信号类型对应的对齐策略；未登记信号按连续模拟量处理。"""

        with self._lock:
            self._policies[signal] = policy

    def policy(self, signal: str) -> SignalPolicy:
        with self._lock:
            return self._policies.get(signal, SignalPolicy.analog(max_gap_s=self._period_s * 2))

    def signals(self) -> list[str]:
        with self._lock:
            return sorted(self._series)

    def append(self, signal: str, ts: float, value: float) -> ClockIssue | None:
        """追加采样点；时间戳倒退时隔离样本并返回部分失败记录。"""

        moment = float(ts)
        with self._lock:
            previous = self._last_ts.get(signal)
            high_water = self._high_water.get(signal, moment)
            issue: ClockIssue | None = None
            if previous is not None and moment < previous:
                issue = ClockIssue(
                    signal=signal,
                    rollback_ts=moment,
                    previous_ts=previous,
                    high_water_ts=max(high_water, previous),
                    accepted_ts=None,
                )
                self._issues.setdefault(signal, []).append(issue)
                self._last_ts[signal] = moment
                return issue

            if moment < high_water:
                issue = ClockIssue(
                    signal=signal,
                    rollback_ts=moment,
                    previous_ts=previous if previous is not None else moment,
                    high_water_ts=high_water,
                    accepted_ts=None,
                )
                self._issues.setdefault(signal, []).append(issue)
                self._last_ts[signal] = moment
                return issue

            series = self._series.setdefault(signal, [])
            series.append(Sample(moment, float(value)))
            if len(series) > self._limit:
                del series[: len(series) - self._limit]
            self._last_ts[signal] = moment
            self._high_water[signal] = moment
            return None

    def latest(self, signal: str) -> Sample | None:
        """返回某信号的最后一个可信样本。"""

        with self._lock:
            series = self._series.get(signal)
            return series[-1] if series else None

    def series(self, signal: str, limit: int = 120) -> list[Sample]:
        """返回某信号最近的若干可信原始样本。"""

        with self._lock:
            series = list(self._series.get(signal, ()))
        return series[-limit:]

    def clock_issues(self, signal: str | None = None) -> list[ClockIssue]:
        """返回时间戳倒退记录；不传信号名时返回全部。"""

        with self._lock:
            if signal is not None:
                return list(self._issues.get(signal, ()))
            return [issue for issues in self._issues.values() for issue in issues]

    def aligned(
        self,
        signal: str,
        start_s: float,
        end_s: float,
        grid: TimeGrid | None = None,
        policy: SignalPolicy | None = None,
    ) -> list:
        """按炉段时间范围对齐一个信号，绝不跨炉界使用样本。"""

        selected_grid = grid or TimeGrid(self._period_s)
        selected_policy = policy or self.policy(signal)
        samples = [(sample.ts, sample.value) for sample in self.series(signal, limit=self._limit)]
        return align_signal(
            signal,
            samples,
            selected_grid,
            start_s,
            end_s,
            selected_policy,
            self.clock_issues(signal),
        )

    def fill_gaps(self, signal: str, samples: list[Sample]) -> list[Sample]:
        """对丢点周期做线性补点，保持旧趋势接口按固定周期展示。"""

        if len(samples) < 2:
            return samples
        filled: list[Sample] = [samples[0]]
        for previous, current in zip(samples, samples[1:]):
            steps = int(round((current.ts - previous.ts) / self._period_s))
            if steps <= 1:
                filled.append(current)
                continue
            for index in range(1, steps):
                ratio = index / steps
                ts = previous.ts + (current.ts - previous.ts) * ratio
                value = previous.value + (current.value - previous.value) * ratio
                filled.append(Sample(ts, value))
            filled.append(current)
        return filled

    def snapshot(self, limit: int = 8) -> dict:
        """汇总各信号的最新样本，供状态快照读取。"""

        with self._lock:
            signal_names = sorted(set(self._series) | set(self._policies))
        report: dict[str, object] = {}
        for signal in signal_names:
            latest = self.latest(signal)
            issues = self.clock_issues(signal)
            report[signal] = {
                "latest": latest.as_dict() if latest else None,
                "recent": [sample.as_dict() for sample in self.series(signal, limit)],
                "policy": self.policy(signal).as_dict(),
                "clock_rollbacks": len(issues),
                "trusted": not issues,
            }
        return report
