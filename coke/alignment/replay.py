"""按炉回放与一致性核对。

回放与对齐共用同一根时间轴：一炉在网格上就是一个闭区间下标 ``[i0, i1]``，
"跳到某一炉"等价于跳到 ``i0``。每个回放点同时给出该时刻的审计记录，趋势
与操作顺序在同一张时间轴上逐格对照。

:func:`verify_alignment` 是审计回放侧的一致性闸门，核对五件事：

1. 产物自带 ``digest`` 与按内容重算的一致（落盘后没被改过）；
2. 网格点严格等间隔、单调递增（统一网格没被破坏）；
3. 每路信号的点数等于网格点数（没有哪路信号偷跑另一根轴）；
4. 每炉边界能稳定映射到网格上的非空区间、边界不回退（切段时序守得住；
   炉界来自审计事件的任意时刻，不要求恰好落在格点上，按夹取规则落格）；
5. 炉段内的审计记录时间戳都落在区间内（按炉过滤与时间轴一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..audit.record import AuditRecord
from .grid import TimeGrid
from .heats import Heat, find_heat
from .run import AlignmentRun, canonical_digest
from .signals import SignalAlignment

_EPS = 1e-6


@dataclass(frozen=True)
class HeatReplay:
    """某一炉在统一网格上的回放切片。"""

    heat: Heat
    index_start: int
    index_end: int
    grid_ts: list[float]
    series: list[dict]
    audit: list[dict]
    trusted: bool

    def as_dict(self) -> dict:
        return {
            "heat": self.heat.as_dict(),
            "index_start": self.index_start,
            "index_end": self.index_end,
            "grid_ts": [round(ts, 3) for ts in self.grid_ts],
            "series": self.series,
            "audit": self.audit,
            "trusted": self.trusted,
        }


def _slice_series(series: SignalAlignment, i0: int, i1: int) -> dict:
    return {
        "signal": series.signal,
        "kind": series.kind,
        "policy": series.policy,
        "points": [point.as_dict() for point in series.points[i0 : i1 + 1]],
    }


def replay_heat(
    run: AlignmentRun,
    heat_id: str,
    audit_records: list[AuditRecord],
) -> HeatReplay:
    """切出某一炉的网格区间、各路信号切片与该炉审计记录。"""

    heat = find_heat(run.cutting, heat_id)
    if heat is None:
        raise KeyError(f"未知炉号 {heat_id!r}")

    # 边界倒退（end<start）的炉只退回到起点单点，保证区间非空、时序不翻转，
    # 整炉可信性由 heat.trusted 与逐点原因体现。
    grid_end = run.grid.ts_at(run.grid.count - 1)
    end_ts = heat.end_ts if heat.end_ts is not None else grid_end
    if end_ts < heat.start_ts:
        end_ts = heat.start_ts
    i0, i1 = run.grid.cover_indices(heat.start_ts, end_ts)
    grid_ts = [run.grid.ts_at(index) for index in range(i0, i1 + 1)]

    selected = [
        entry
        for entry in audit_records
        if entry.oven == heat.oven and heat.start_ts - _EPS <= entry.ts <= end_ts + _EPS
    ]
    selected.sort(key=lambda entry: entry.ts)

    series_trusted = all(
        all(point.trusted for point in item.points[i0 : i1 + 1]) for item in run.series
    )
    return HeatReplay(
        heat=heat,
        index_start=i0,
        index_end=i1,
        grid_ts=grid_ts,
        series=[_slice_series(item, i0, i1) for item in run.series],
        audit=[entry.as_dict() for entry in selected],
        trusted=heat.trusted and series_trusted,
    )


def list_heat_jumps(run: AlignmentRun) -> list[dict]:
    """返回回放跳转表：每炉一行，给炉号与起始网格下标。"""

    return [
        {
            "heat_id": heat.heat_id,
            "oven": heat.oven,
            "start_ts": round(heat.start_ts, 3),
            "end_ts": None if heat.end_ts is None else round(heat.end_ts, 3),
            "index_start": run.grid.clamp_index(heat.start_ts),
            "trusted": heat.trusted,
            "anomalies": list(heat.anomalies),
        }
        for heat in run.cutting.heats
    ]


@dataclass(frozen=True)
class Verification:
    """对齐结果与审计回放的一致性核对结论。"""

    ok: bool
    digest_match: bool
    grid_monotonic: bool
    series_axes_match: bool
    boundaries_mapped: bool
    boundary_order_ok: bool
    audit_within_heats: bool
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "digest_match": self.digest_match,
            "grid_monotonic": self.grid_monotonic,
            "series_axes_match": self.series_axes_match,
            "boundaries_mapped": self.boundaries_mapped,
            "boundary_order_ok": self.boundary_order_ok,
            "audit_within_heats": self.audit_within_heats,
            "problems": self.problems,
        }


def _grid_is_monotonic(grid: TimeGrid) -> bool:
    if grid.period_s <= 0 or grid.count < 1:
        return False
    return all(
        abs(grid.ts_at(index) - (grid.origin + index * grid.period_s)) <= _EPS
        for index in range(grid.count)
    )


def _boundaries_mapped(run: AlignmentRun) -> bool:
    """每炉边界都能映射成网格上的非空区间，同炉室炉段不回退交叠。"""

    grid_end = run.grid.ts_at(run.grid.count - 1)
    last_end: dict[str, float] = {}
    for heat in run.cutting.heats:
        if heat.end_ts is not None and heat.end_ts < heat.start_ts:
            return False  # 边界倒退：cover_indices 会夹成非空，必须显式拒绝。
        i0, i1 = run.grid.cover_indices(heat.start_ts, heat.end_ts if heat.end_ts is not None else grid_end)
        if i1 < i0:
            return False
        previous = last_end.get(heat.oven, run.grid.origin)
        if heat.end_ts is not None:
            if heat.end_ts < previous - _EPS:
                return False
            last_end[heat.oven] = max(previous, heat.end_ts)
    return True


def verify_alignment(run: AlignmentRun, audit_records: list[AuditRecord]) -> Verification:
    """按同一时间轴核对对齐产物与审计记录的一致性。"""

    problems: list[str] = []

    recomputed = canonical_digest(run.run_id, run.as_dict(full_precision=True))
    digest_match = recomputed == run.digest
    if not digest_match:
        problems.append("digest 与按内容重算结果不一致")

    grid_monotonic = _grid_is_monotonic(run.grid)
    if not grid_monotonic:
        problems.append("网格点非等间隔或非单调")

    series_axes_match = all(len(item.points) == run.grid.count for item in run.series)
    if not series_axes_match:
        problems.append("存在点数与网格点数不一致的信号")

    boundaries_mapped = _boundaries_mapped(run)
    if not boundaries_mapped:
        problems.append("炉界无法映射为非空网格区间或同炉室炉段回退")

    boundary_order_ok = all(
        heat.end_ts is None or heat.end_ts >= heat.start_ts for heat in run.cutting.heats
    )
    for heat in run.cutting.heats:
        if heat.end_ts is not None and heat.end_ts < heat.start_ts:
            problems.append(f"{heat.heat_id} 结束早于开始")

    # 审计记录与炉段同轴：属于某炉室的记录，时间戳不得早于该炉室首炉起点。
    audit_within_heats = True
    first_start: dict[str, float] = {}
    for heat in run.cutting.heats:
        first_start[heat.oven] = min(first_start.get(heat.oven, heat.start_ts), heat.start_ts)
    for record in audit_records:
        earliest = first_start.get(record.oven)
        if earliest is not None and record.ts < earliest - _EPS:
            audit_within_heats = False
            problems.append(f"{record.oven} 审计 {round(record.ts, 3)} 早于首炉开始")

    ok = (
        digest_match
        and grid_monotonic
        and series_axes_match
        and boundaries_mapped
        and boundary_order_ok
        and audit_within_heats
    )
    return Verification(
        ok=ok,
        digest_match=digest_match,
        grid_monotonic=grid_monotonic,
        series_axes_match=series_axes_match,
        boundaries_mapped=boundaries_mapped,
        boundary_order_ok=boundary_order_ok,
        audit_within_heats=audit_within_heats,
        problems=problems,
    )
