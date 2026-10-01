"""信号对齐：把各采各的样本落到统一时间网格。

对齐策略按信号类型定死，只提供两种，调用方不能在策略外再夹带别的插值口径：

* ``last_hold``（最近点前插，模拟量）：网格点取"不晚于它的最近一个真样本"
  的值。两个真样本之间若拉开超过 ``max_gap_periods`` 个网格周期，超出最近点
  可信度的部分标 ``sample_gap`` 不可信（值仍按前插给出，便于回放时区分"无
  数据"和"值不可信"）；
* ``piecewise_hold``（分段保持，状态量）：同样前插取值，但状态分段保持，
  长时间不变本来就是合法工况，缺采不构成不可信。

两类部分失败都会在对应网格点上留痕、整段不失败：

* 采样时间戳倒退（后到样本 ``ts`` 不晚于已见到的最大时间戳）：倒退样本剔除
  不参与对齐，从倒退点到"时序追上历史最大值"这段标 ``timestamp_regression``；
* 首点之前的网格点没有任何真样本可依：``covered=False``、``value=None``，
  不算不可信，是对齐窗口覆盖不到现场数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .grid import TimeGrid

ANALOG = "analog"
STATE = "state"
POLICY_LAST_HOLD = "last_hold"
POLICY_PIECEWISE_HOLD = "piecewise_hold"
KIND_POLICY = {ANALOG: POLICY_LAST_HOLD, STATE: POLICY_PIECEWISE_HOLD}

REGRESSION = "timestamp_regression"
SAMPLE_GAP = "sample_gap"
DEFAULT_MAX_GAP_PERIODS = 3


@dataclass(frozen=True)
class RawSample:
    """一个到达现场采集侧的原始样本（按到达序喂给对齐）。"""

    ts: float
    value: float
    seq: int = 0

    def as_dict(self) -> dict:
        return {"ts": round(self.ts, 3), "value": self.value, "seq": self.seq}


@dataclass(frozen=True)
class AlignedPoint:
    """统一网格上的一个对齐点。"""

    index: int
    ts: float
    value: float | None
    covered: bool
    trusted: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    source_ts: float | None = None

    def as_dict(self, *, full_precision: bool = False) -> dict:
        return {
            "index": self.index,
            "ts": self.ts if full_precision else round(self.ts, 3),
            "value": None if self.value is None else (self.value if full_precision else round(self.value, 4)),
            "covered": self.covered,
            "trusted": self.trusted,
            "reasons": list(self.reasons),
            "source_ts": (
                None if self.source_ts is None else (self.source_ts if full_precision else round(self.source_ts, 3))
            ),
        }


def policy_for_kind(kind: str) -> str:
    """按信号类型返回对齐策略，未知类型不允许静默归类。"""

    try:
        return KIND_POLICY[kind]
    except KeyError as error:
        raise ValueError(f"未知信号类型 {kind!r}，可选: {sorted(KIND_POLICY)}") from error


@dataclass(frozen=True)
class SignalAlignment:
    """一路信号对齐到统一网格的完整结果。"""

    signal: str
    kind: str
    policy: str
    points: list[AlignedPoint]
    regressions: list[dict] = field(default_factory=list)
    trusted_points: int = 0
    untrusted_points: int = 0
    uncovered_points: int = 0

    def as_dict(self, *, full_precision: bool = False) -> dict:
        return {
            "signal": self.signal,
            "kind": self.kind,
            "policy": self.policy,
            "trusted_points": self.trusted_points,
            "untrusted_points": self.untrusted_points,
            "uncovered_points": self.uncovered_points,
            "regressions": [
                {
                    "ts": item["ts"] if full_precision else round(item["ts"], 3),
                    "last_ts": item["last_ts"] if full_precision else round(item["last_ts"], 3),
                    "seq": item["seq"],
                }
                for item in self.regressions
            ],
            "points": [point.as_dict(full_precision=full_precision) for point in self.points],
        }


def _mark_regression_span(untrusted: list[set[str]], grid: TimeGrid, start_ts: float, end_ts: float) -> None:
    """标记倒退影响区间 ``[start_ts, end_ts)`` 内的网格点。

    ``end_ts`` 上的格点不标记——该时刻已有一条时序正常的可信真样本；起点若
    恰落在格点上则标记（倒退样本就声称在这一刻）。
    """

    i0 = grid.index_of(start_ts)
    if grid.ts_at(i0) < start_ts - 1e-9:
        i0 += 1
    i1 = grid.index_of(end_ts)
    if abs(grid.ts_at(i1) - end_ts) <= 1e-9:
        i1 -= 1
    if i0 > i1:
        return
    i0 = min(max(i0, 0), grid.count - 1)
    i1 = min(max(i1, 0), grid.count - 1)
    for index in range(i0, i1 + 1):
        untrusted[index].add(REGRESSION)


def align_signal(
    signal: str,
    kind: str,
    raw_samples: list[RawSample],
    grid: TimeGrid,
    max_gap_periods: int = DEFAULT_MAX_GAP_PERIODS,
) -> SignalAlignment:
    """把一路信号的原始样本按其类型对齐到统一网格。

    输入样本按到达序解释（不预先排序），这样时间戳倒退才能被检出而不是被
    排序悄悄抹掉。输出逐网格点给出值、覆盖、可信与原因。
    """

    policy = policy_for_kind(kind)
    max_gap_periods = max(0, int(max_gap_periods))

    # 到达序扫描：保留时序单调的真样本，记录倒退点与其影响区间。
    accepted: list[RawSample] = []
    regressions: list[dict] = []
    untrusted: list[set[str]] = [set() for _ in range(grid.count)]
    high_water = float("-inf")
    for sample in raw_samples:
        if accepted and sample.ts <= high_water:
            regressions.append(
                {"ts": float(sample.ts), "last_ts": float(high_water), "seq": sample.seq}
            )
            # 倒退样本之后直到时序追上历史最大值前都不可信；high_water 时刻
            # 本身有可信真样本，按半开区间不标记该格点。
            _mark_regression_span(untrusted, grid, sample.ts, high_water)
            continue
        accepted.append(sample)
        high_water = max(high_water, sample.ts)

    # 真样本按时间升序（accepted 内部已单调）；同刻点取后者。
    ordered: list[RawSample] = []
    for sample in accepted:
        if ordered and sample.ts == ordered[-1].ts:
            ordered[-1] = sample
        else:
            ordered.append(sample)

    points: list[AlignedPoint] = []
    trusted_points = 0
    untrusted_points = 0
    uncovered_points = 0
    cursor = 0
    for index in range(grid.count):
        ts = grid.ts_at(index)
        while cursor < len(ordered) and ordered[cursor].ts <= ts + 1e-9:
            cursor += 1
        reasons = set(untrusted[index])
        if cursor == 0:
            points.append(AlignedPoint(index, ts, None, False, True, (), None))
            uncovered_points += 1
            continue
        source = ordered[cursor - 1]
        # 模拟量缺采：前插值所依真样本的年龄达到容忍跨度上限即不可信（内部
        # 缺口与末尾拖尾同一口径）。状态量分段保持，陈旧合法，不判缺采。
        if policy == POLICY_LAST_HOLD and (ts - source.ts) >= max_gap_periods * grid.period_s - 1e-9:
            reasons.add(SAMPLE_GAP)
        reasons = tuple(sorted(reasons))
        trusted = not reasons
        if trusted:
            trusted_points += 1
        else:
            untrusted_points += 1
        points.append(AlignedPoint(index, ts, source.value, True, trusted, reasons, source.ts))

    return SignalAlignment(
        signal=signal,
        kind=kind,
        policy=policy,
        points=points,
        regressions=regressions,
        trusted_points=trusted_points,
        untrusted_points=untrusted_points,
        uncovered_points=uncovered_points,
    )
