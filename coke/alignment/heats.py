"""按炉切段。

切段口径定死：**按炉界，不按班次**。一炉是同一碳化室（``oven``）的一次
冶炼周期，从装煤完成（``charge_complete``，``ok=true``）开始，到推焦完成
（``push_complete``，``ok=true``）结束；同炉室的开始／结束按 FIFO 配对，
不同炉室允许时间交叠（两座炉本来就在并行作业）。

炉界是回放能"跳到某一炉"的前提，因此边界时序必须守得住，守不住的部分照
切但要标出来、整炉记不可信（部分失败，不丢段）：

* ``orphan_end``      没有开始记录的推焦完成（审计缺段或先于对齐窗口）；
* ``unterminated``    只有装煤没有推焦（在炼的一炉，窗口结束仍未出焦）；
* ``boundary_regression`` 结束早于开始；
* ``heat_overlap``    同一炉室两炉时间交叠（FIFO 没配上的倒挂）。

输入是 :class:`~coke.audit.record.AuditRecord` 列表，口径与审计时间线一致。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from ..audit.record import AuditRecord

START_ACTION = "charge_complete"
END_ACTION = "push_complete"

ORPHAN_END = "orphan_end"
UNTERMINATED = "unterminated"
BOUNDARY_REGRESSION = "boundary_regression"
HEAT_OVERLAP = "heat_overlap"


@dataclass(frozen=True)
class Heat:
    """一炉：某炉室从装煤完成到推焦完成的半开半闭工况段。"""

    heat_id: str
    oven: str
    seq: int
    start_ts: float
    end_ts: float | None
    start_action_ts: float
    end_action_ts: float | None = None
    trusted: bool = True
    anomalies: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self, *, full_precision: bool = False) -> dict:
        def r(value: float | None) -> float | None:
            if value is None:
                return None
            return value if full_precision else round(value, 3)

        return {
            "heat_id": self.heat_id,
            "oven": self.oven,
            "seq": self.seq,
            "start_ts": r(self.start_ts),
            "end_ts": r(self.end_ts),
            "trusted": self.trusted,
            "anomalies": list(self.anomalies),
        }


@dataclass(frozen=True)
class HeatCutting:
    """按炉切段的完整结果。"""

    heats: list[Heat]
    anomalies: list[dict] = field(default_factory=list)

    def as_dict(self, *, full_precision: bool = False) -> dict:
        def r(value: float) -> float:
            return value if full_precision else round(value, 3)

        normalized = []
        for item in self.anomalies:
            entry = dict(item)
            for key in ("ts", "start_ts", "prev_end_ts"):
                if key in entry and entry[key] is not None:
                    entry[key] = r(float(entry[key]))
            normalized.append(entry)
        return {
            "boundary": "heat",
            "count": len(self.heats),
            "anomalies": normalized,
            "heats": [heat.as_dict(full_precision=full_precision) for heat in self.heats],
        }


def _is_ok(record: AuditRecord, action: str) -> bool:
    return record.action == action and bool(record.payload.get("ok", False))


def cut_heats(records: list[AuditRecord]) -> HeatCutting:
    """从审计记录按炉界切段。

    开始／结束按审计流水的到达序（journal 顺序）FIFO 配对，不能按时间戳
    排序——排序会把现场时钟倒挂（推焦事件的时间戳早于装煤）悄悄改写成正常
    顺序，倒退就检不出来了。是否倒退交由配对后的边界时间戳单独判定；最终
    炉段列表再按开始时间戳排序供展示。
    """

    opens: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
    counters: dict[str, int] = defaultdict(int)
    heats: list[Heat] = []
    anomalies: list[dict] = []

    for record in records:
        oven = record.oven
        if _is_ok(record, START_ACTION):
            counters[oven] += 1
            opens[oven].append((record.ts, counters[oven]))
            continue
        if not _is_ok(record, END_ACTION):
            continue

        if not opens[oven]:
            anomalies.append(
                {"oven": oven, "ts": float(record.ts), "reason": ORPHAN_END}
            )
            continue

        start_ts, seq = opens[oven].popleft()
        reasons: list[str] = []
        if record.ts < start_ts:
            reasons.append(BOUNDARY_REGRESSION)
        heats.append(
            Heat(
                heat_id=f"{oven}#{seq}",
                oven=oven,
                seq=seq,
                start_ts=start_ts,
                end_ts=record.ts,
                start_action_ts=start_ts,
                end_action_ts=record.ts,
                trusted=not reasons,
                anomalies=tuple(reasons),
            )
        )

    # 在炼炉：窗口结束仍未推焦完成，段保留到最后一个已知时刻（start_ts）。
    for oven, pending in opens.items():
        for start_ts, seq in pending:
            anomalies.append(
                {"oven": oven, "heat_id": f"{oven}#{seq}", "ts": float(start_ts), "reason": UNTERMINATED}
            )
            heats.append(
                Heat(
                    heat_id=f"{oven}#{seq}",
                    oven=oven,
                    seq=seq,
                    start_ts=start_ts,
                    end_ts=None,
                    start_action_ts=start_ts,
                    trusted=True,
                    anomalies=(UNTERMINATED,),
                )
            )

    heats.sort(key=lambda heat: heat.start_ts)

    # 时序守界：同一炉室各炉不得交叠；交叠说明开始／结束没按 FIFO 对上。
    last_end: dict[str, float] = {}
    marked: list[Heat] = []
    for heat in heats:
        previous_end = last_end.get(heat.oven)
        if previous_end is not None and heat.start_ts < previous_end:
            anomalies.append(
                {
                    "oven": heat.oven,
                    "heat_id": heat.heat_id,
                    "start_ts": float(heat.start_ts),
                    "prev_end_ts": float(previous_end),
                    "reason": HEAT_OVERLAP,
                }
            )
            if HEAT_OVERLAP not in heat.anomalies:
                heat = Heat(
                    heat_id=heat.heat_id,
                    oven=heat.oven,
                    seq=heat.seq,
                    start_ts=heat.start_ts,
                    end_ts=heat.end_ts,
                    start_action_ts=heat.start_action_ts,
                    end_action_ts=heat.end_action_ts,
                    trusted=False,
                    anomalies=tuple(sorted((*heat.anomalies, HEAT_OVERLAP))),
                )
        marked.append(heat)
        if heat.end_ts is not None:
            last_end[heat.oven] = max(previous_end or heat.end_ts, heat.end_ts)

    return HeatCutting(heats=marked, anomalies=anomalies)


def find_heat(cutting: HeatCutting, heat_id: str) -> Heat | None:
    """按 ``oven#seq`` 形式的炉号定位一段。"""

    for heat in cutting.heats:
        if heat.heat_id == heat_id:
            return heat
    return None
