"""按炉界切段与审计时间轴回放。

切段口径固定为炉界，不按班次界：装煤完成是一炉开始，熄焦完成是一炉结束。
炉界时间戳直接取自审计记录，不吸附到趋势网格，因此审计事件与对齐结果始终
使用同一条 Unix 秒级时间轴核对。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..audit.record import AuditRecord
from ..eventbus import Event
from .grid import AlignedPoint, BAD_QUALITIES, ClockIssue, TimeGrid, align_signal

if TYPE_CHECKING:
    from ..eventbus import EventBus
    from ..store.jsonfile import JsonStore
    from .telemetry import TrendBuffer

SEGMENT_BASIS = "heat"
START_ACTION = "charge_complete"
END_ACTION = "quench_complete"


@dataclass(frozen=True)
class HeatSegment:
    """一炉工况的半开时间区间，炉界时间保持原始审计时间戳。"""

    heat_id: str
    heat_no: int
    oven: str
    start_ts: float
    end_ts: float | None
    open: bool
    status: str
    warnings: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        """炉界本身是否可信；数据点可信度另行统计。"""

        return not self.warnings

    def as_dict(self) -> dict:
        return {
            "heat_id": self.heat_id,
            "heat_no": self.heat_no,
            "oven": self.oven,
            "start_ts": round(self.start_ts, 3),
            "end_ts": None if self.end_ts is None else round(self.end_ts, 3),
            "open": self.open,
            "status": self.status,
            "trusted": self.trusted,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class HeatSegmentation:
    """全部炉段与无法归入正常炉段的边界告警。"""

    segments: tuple[HeatSegment, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "segment_basis": SEGMENT_BASIS,
            "segments": [segment.as_dict() for segment in self.segments],
            "warnings": list(self.warnings),
        }

    def find(self, heat_id: str) -> HeatSegment | None:
        return next((segment for segment in self.segments if segment.heat_id == heat_id), None)


def build_heat_segments(records: list[AuditRecord]) -> HeatSegmentation:
    """从装煤、熄焦完成事件构造炉段；班次事件不参与切段。"""

    boundaries = [
        record
        for record in sorted(records, key=lambda item: item.ts)
        if record.action in (START_ACTION, END_ACTION) and record.oven
    ]
    active: dict[str, HeatSegment] = {}
    segments: list[HeatSegment] = []
    warnings: list[str] = []
    heat_no = 0

    for record in boundaries:
        oven = record.oven
        operation_ok = record.payload.get("ok", True) is not False
        if record.action == START_ACTION:
            if not operation_ok:
                warnings.append(f"{oven} 在 {round(record.ts, 3)} 的装煤未完成，不作为炉界")
                continue
            heat_no += 1
            if oven in active:
                previous = active.pop(oven)
                closed = HeatSegment(
                    heat_id=previous.heat_id,
                    heat_no=previous.heat_no,
                    oven=previous.oven,
                    start_ts=previous.start_ts,
                    end_ts=record.ts,
                    open=False,
                    status="boundary_conflict",
                    warnings=(*previous.warnings, "missing_quench_closed_at_next_charge"),
                )
                segments.append(closed)
                warnings.append(f"{closed.heat_id} 缺少熄焦结束，已在下一炉装煤界关闭")
            segment = HeatSegment(
                heat_id=f"heat-{heat_no:04d}",
                heat_no=heat_no,
                oven=oven,
                start_ts=record.ts,
                end_ts=None,
                open=True,
                status="open",
            )
            active[oven] = segment
            segments.append(segment)
            continue

        current = active.get(oven)
        if current is None:
            warnings.append(f"{oven} 在 {round(record.ts, 3)} 出现孤立熄焦界，未找到装煤开始")
            continue
        if not operation_ok:
            warnings.append(f"{current.heat_id} 的熄焦未完成，炉段保持开放")
            continue
        active.pop(oven, None)
        end_ts = record.ts
        segment_warnings = list(current.warnings)
        if end_ts < current.start_ts:
            segment_warnings.append("quench_before_charge")
            warnings.append(f"{current.heat_id} 熄焦时间早于装煤时间")
        elif end_ts == current.start_ts:
            segment_warnings.append("zero_duration_heat")
        closed = HeatSegment(
            heat_id=current.heat_id,
            heat_no=current.heat_no,
            oven=current.oven,
            start_ts=current.start_ts,
            end_ts=end_ts,
            open=False,
            status="closed" if not segment_warnings else "warning",
            warnings=tuple(segment_warnings),
        )
        segments[segments.index(current)] = closed

    return HeatSegmentation(tuple(segments), tuple(warnings))


def _count_qualities(points: list[AlignedPoint]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for point in points:
        counts[point.quality] = counts.get(point.quality, 0) + 1
    return dict(sorted(counts.items()))


def _checksum(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HeatReplay:
    """生成、落盘并校验某一炉的统一时间轴回放清单。"""

    def __init__(
        self,
        trend: "TrendBuffer",
        bus: "EventBus",
        store: "JsonStore",
        grid: TimeGrid | None = None,
    ) -> None:
        self._trend = trend
        self._bus = bus
        self._store = store
        self._grid = grid or TimeGrid(period_s=trend.period_s)

    def segmentation(self, records: list[AuditRecord] | None = None) -> HeatSegmentation:
        if records is None:
            records = []
        return build_heat_segments(records)

    def build(
        self,
        segment: HeatSegment,
        records: list[AuditRecord],
        signals: list[str] | None = None,
        created_ts: float | None = None,
        persist: bool = True,
    ) -> dict:
        """生成一炉的对齐结果；开放炉段截到当前最后一条审计时间。"""

        selected_signals = list(self._trend.signals()) if signals is None else signals
        end_ts = segment.end_ts
        if end_ts is None:
            record_times = [
                record.ts
                for record in records
                if record.oven == segment.oven and record.ts >= segment.start_ts
            ]
            end_ts = max(record_times, default=segment.start_ts)
        if end_ts < segment.start_ts:
            end_ts = segment.start_ts

        timeline = [
            {"step": index + 1, **record.as_dict()}
            for index, record in enumerate(
                sorted(
                    (
                        record
                        for record in records
                        if record.oven == segment.oven
                        and segment.start_ts <= record.ts <= end_ts
                        and not (record.action == START_ACTION and record.ts == end_ts and segment.open is False)
                    ),
                    key=lambda item: item.ts,
                )
            )
        ]
        aligned_signals: dict[str, dict] = {}
        all_issues: list[ClockIssue] = []
        total_points = 0
        bad_points = 0
        for signal in selected_signals:
            samples = self._trend.series(signal, limit=10_000_000)
            issues = [
                issue
                for issue in self._trend.clock_issues(signal)
                if issue.overlaps(segment.start_ts, end_ts)
            ]
            all_issues.extend(issues)
            points = align_signal(
                signal,
                [(sample.ts, sample.value) for sample in samples],
                self._grid,
                segment.start_ts,
                end_ts,
                self._trend.policy(signal),
                issues,
            )
            counts = _count_qualities(points)
            total_points += len(points)
            bad_points += sum(count for quality, count in counts.items() if quality in BAD_QUALITIES)
            aligned_signals[signal] = {
                "policy": self._trend.policy(signal).as_dict(),
                "points": [point.as_dict() for point in points],
                "counts": counts,
                "clock_issues": [
                    issue.as_dict()
                    for issue in issues
                    if segment.start_ts <= issue.end_ts and issue.start_ts <= end_ts
                ],
            }

        warnings = list(segment.warnings)
        if all_issues:
            warnings.append("clock_rollback")
        if bad_points:
            warnings.append("missing_or_untrusted_samples")
        audit_start = timeline[0]["ts"] if timeline else round(segment.start_ts, 3)
        audit_end = timeline[-1]["ts"] if timeline else round(end_ts, 3)
        grid_points = self._grid.points(segment.start_ts, end_ts)
        payload = {
            "schema": "coke.heat_replay.v1",
            "segment_basis": SEGMENT_BASIS,
            "heat": segment.as_dict(),
            "time_axis": {
                "time_zone": "utc",
                "unit": "unix_seconds",
                "grid_period_s": self._grid.period_s,
                "grid_origin_s": self._grid.origin_s,
                "start_ts": round(segment.start_ts, 3),
                "end_ts": round(end_ts, 3),
                "grid_first_ts": round(grid_points[0], 3) if grid_points else None,
                "grid_last_ts": round(grid_points[-1], 3) if grid_points else None,
                "audit_first_ts": audit_start,
                "audit_last_ts": audit_end,
            },
            "signals": aligned_signals,
            "timeline": timeline,
            "quality": {
                "points": total_points,
                "bad_points": bad_points,
                "trusted": not warnings,
                "partial_failure": bool(warnings),
            },
            "warnings": warnings,
            "created_ts": created_ts,
        }
        manifest = {**payload, "checksum": _checksum(payload)}
        if persist:
            self._store.write(f"heat_replay_{segment.heat_id}", manifest)
            self._bus.publish(
                Event.of(
                    "timeseries",
                    "timeseries",
                    "heat_replay_built",
                    severity="notice" if manifest["quality"]["trusted"] else "warning",
                    message=f"炉段 {segment.heat_id} 回放已生成",
                    heat_id=segment.heat_id,
                    oven=segment.oven,
                    start_ts=round(segment.start_ts, 3),
                    end_ts=round(end_ts, 3),
                    bad_points=bad_points,
                    checksum=manifest["checksum"],
                )
            )
        return manifest

    def load(self, heat_id: str) -> dict | None:
        return self._store.read(f"heat_replay_{heat_id}")

    def verify(self, manifest: dict) -> dict:
        """按同一时间轴和校验和核对回放清单是否被改动。"""

        stored_checksum = str(manifest.get("checksum", ""))
        payload = {key: value for key, value in manifest.items() if key != "checksum"}
        actual_checksum = _checksum(payload)
        axis = manifest.get("time_axis", {})
        signals = manifest.get("signals", {})
        axis_ok = (
            axis.get("unit") == "unix_seconds"
            and axis.get("time_zone") == "utc"
            and isinstance(signals, dict)
        )
        for signal in signals.values():
            points = signal.get("points", []) if isinstance(signal, dict) else []
            timestamps = [point.get("ts") for point in points if isinstance(point, dict)]
            if timestamps != sorted(timestamps):
                axis_ok = False
        checksum_ok = stored_checksum == actual_checksum
        return {
            "ok": checksum_ok and axis_ok,
            "checksum_ok": checksum_ok,
            "axis_ok": axis_ok,
            "expected_checksum": stored_checksum,
            "actual_checksum": actual_checksum,
        }
