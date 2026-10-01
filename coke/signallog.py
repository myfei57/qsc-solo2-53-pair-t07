"""现场原始信号日志。

对齐的输入是各信号各自采样的原始流：每路信号按自己的节奏上报，时间戳
可能倒退（采集站重发／校时）、可能整段缺失。:class:`SignalLog` 只负责按
到达序把原始样本攒下来并登记每路信号的类型（模拟量／状态量），不做任何
排序或补点——一旦在这里排序，倒退与缺失就再也检不出来。对齐与按炉切段
统一在 :mod:`coke.alignment` 里完成。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .alignment.signals import KIND_POLICY, RawSample, policy_for_kind


@dataclass(frozen=True)
class SignalInfo:
    """一路已登记信号的元数据。"""

    signal: str
    kind: str
    policy: str
    max_gap_periods: int

    def as_dict(self) -> dict:
        return {
            "signal": self.signal,
            "kind": self.kind,
            "policy": self.policy,
            "max_gap_periods": self.max_gap_periods,
        }


class SignalLog:
    """按到达序保存各信号原始样本的有限长度日志。"""

    def __init__(self, default_max_gap_periods: int = 3, limit: int = 8192) -> None:
        self._samples: dict[str, list[RawSample]] = {}
        self._kinds: dict[str, SignalInfo] = {}
        self._limit = max(1, limit)
        self._default_max_gap_periods = max(0, int(default_max_gap_periods))
        self._seq = 0
        self._lock = threading.RLock()

    def register(self, signal: str, kind: str, max_gap_periods: int | None = None) -> SignalInfo:
        """登记一路信号的类型；重复登记必须与原类型一致，不允许改口。"""

        if kind not in KIND_POLICY:
            raise ValueError(f"未知信号类型 {kind!r}，可选: {sorted(KIND_POLICY)}")
        gap = self._default_max_gap_periods if max_gap_periods is None else max(0, int(max_gap_periods))
        with self._lock:
            existing = self._kinds.get(signal)
            if existing is not None and (existing.kind != kind or existing.max_gap_periods != gap):
                raise ValueError(f"信号 {signal!r} 已按类型 {existing.kind!r} 登记，不能改口")
            info = SignalInfo(signal=signal, kind=kind, policy=policy_for_kind(kind), max_gap_periods=gap)
            self._kinds[signal] = info
            self._samples.setdefault(signal, [])
            return info

    def ingest(self, signal: str, ts: float, value: float, kind: str | None = None) -> RawSample:
        """按到达序收录一个原始样本；未登记信号必须随样本给出类型。"""

        with self._lock:
            if signal not in self._kinds:
                if kind is None:
                    raise ValueError(f"未登记信号 {signal!r}，首条样本必须带信号类型")
                self.register(signal, kind)
            self._seq += 1
            sample = RawSample(ts=float(ts), value=float(value), seq=self._seq)
            series = self._samples[signal]
            series.append(sample)
            if len(series) > self._limit:
                del series[: len(series) - self._limit]
            return sample

    def samples(self, signal: str) -> list[RawSample]:
        """返回一路信号的原始样本（到达序拷贝）。"""

        with self._lock:
            return list(self._samples.get(signal, ()))

    def info(self, signal: str) -> SignalInfo | None:
        with self._lock:
            return self._kinds.get(signal)

    def signals(self) -> list[SignalInfo]:
        """返回全部已登记信号（按信号名排序）。"""

        with self._lock:
            return [self._kinds[name] for name in sorted(self._kinds)]

    def span(self) -> tuple[float, float] | None:
        """返回全部样本的最小/最大时间戳；没有样本时返回 ``None``。"""

        with self._lock:
            timestamps = [sample.ts for series in self._samples.values() for sample in series]
        if not timestamps:
            return None
        return min(timestamps), max(timestamps)

    def summary(self) -> dict:
        """供控制台状态页展示的登记与样本计数摘要。"""

        with self._lock:
            infos = list(self._kinds.values())
            counts = {name: len(series) for name, series in self._samples.items()}
        return {
            "signals": [info.as_dict() for info in sorted(infos, key=lambda item: item.signal)],
            "sample_counts": counts,
            "total_samples": sum(counts.values()),
        }
