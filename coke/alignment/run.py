"""对齐编排：原始样本 + 审计炉界 → 统一网格上的可回放结果。

:func:`build_alignment` 是"对齐一次"的唯一入口：先按炉界切段（口径见
:mod:`coke.alignment.heats`），再以一个固定锚点铺统一网格（口径见
:mod:`coke.alignment.grid`），最后把每路信号按自身类型对齐（口径见
:mod:`coke.alignment.signals`）。网格是整次对齐里所有炉、所有信号共用的一
根轴，因此"回放某一炉"只是在这根轴上切一个下标区间，回放与审计能按同一
时间轴核对。

结果自带 ``digest``（对内容的规范化 JSON 取 sha256，摘要字段自身除外），
落审计流水后，核对方拿同一份输入重算应得到同一摘要，否则就是对齐结果或
审计流水被改动过。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from ..audit.record import AuditRecord
from .grid import TimeGrid
from .heats import Heat, HeatCutting, cut_heats
from .signals import (
    DEFAULT_MAX_GAP_PERIODS,
    REGRESSION,
    SAMPLE_GAP,
    AlignedPoint,
    RawSample,
    SignalAlignment,
    align_signal,
)

DIGEST_PREFIX = "sha256:"


@dataclass(frozen=True)
class SignalSpec:
    """一路待对齐信号的类型与缺采容忍度。"""

    signal: str
    kind: str
    max_gap_periods: int = DEFAULT_MAX_GAP_PERIODS


@dataclass(frozen=True)
class AlignmentRun:
    """一次对齐的完整产物。"""

    run_id: str
    grid: TimeGrid
    window: dict
    cutting: HeatCutting
    series: list[SignalAlignment]
    anomalies: list[dict] = field(default_factory=list)
    digest: str = ""

    def as_dict(self, *, full_precision: bool = False) -> dict:
        def r(value: float) -> float:
            return value if full_precision else round(value, 3)

        return {
            "run_id": self.run_id,
            "window": {
                "start_ts": r(float(self.window["start_ts"])),
                "end_ts": r(float(self.window["end_ts"])),
                "period_s": self.window["period_s"],
            },
            "grid": self.grid.as_dict(full_precision=full_precision),
            "heats": self.cutting.as_dict(full_precision=full_precision),
            "anomalies": [
                self._anomaly_dict(item, full_precision) for item in self.anomalies
            ],
            "series": [item.as_dict(full_precision=full_precision) for item in self.series],
            "digest": self.digest,
        }

    @staticmethod
    def _anomaly_dict(item: dict, full_precision: bool) -> dict:
        entry = dict(item)
        for key in ("ts", "last_ts", "from_ts", "to_ts", "start_ts", "prev_end_ts"):
            if key in entry and entry[key] is not None:
                entry[key] = float(entry[key]) if full_precision else round(float(entry[key]), 3)
        return entry


def canonical_digest(run_id: str, payload: dict) -> str:
    """对不含 ``digest`` 字段的产物内容取规范化 sha256。"""

    body = copy.deepcopy(payload)
    body.pop("digest", None)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return DIGEST_PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _series_anomalies(signal: str, alignment: SignalAlignment) -> list[dict]:
    found: list[dict] = []
    for regression in alignment.regressions:
        found.append({"signal": signal, "reason": REGRESSION, **regression})
    gap_points = [point for point in alignment.points if SAMPLE_GAP in point.reasons]
    if gap_points:
        found.append(
            {
                "signal": signal,
                "reason": SAMPLE_GAP,
                "from_ts": gap_points[0].ts,
                "to_ts": gap_points[-1].ts,
                "points": len(gap_points),
            }
        )
    return found


def build_alignment(
    specs: list[SignalSpec],
    samples: dict[str, list[RawSample]],
    audit_records: list[AuditRecord],
    period_s: float,
    *,
    run_id: str,
    window: tuple[float, float] | None = None,
) -> AlignmentRun:
    """执行一次对齐。

    窗口缺省取"全部信号真样本"的最小/最大时间戳；显式给出时末端不得早于
    起点。审计记录只用于按炉切段，不改变网格窗口。
    """

    if period_s <= 0:
        raise ValueError("对齐周期必须为正")
    if not specs:
        raise ValueError("至少需要一路待对齐信号")

    # 窗口必须同时罩住样本与炉界审计：回放和核对共用同一根轴，若窗口只取
    # 样本范围，早于首个样本的炉界就会落到轴外，核对必然失败。
    all_ts = [sample.ts for series in samples.values() for sample in series]
    all_ts.extend(record.ts for record in audit_records)
    if window is not None:
        start_ts, end_ts = window
    elif all_ts:
        start_ts, end_ts = min(all_ts), max(all_ts)
    else:
        raise ValueError("没有任何样本或审计记录，无法确定对齐窗口")

    grid = TimeGrid.build(start_ts, end_ts, period_s)
    cutting = cut_heats(audit_records)

    series: list[SignalAlignment] = []
    anomalies: list[dict] = list(cutting.anomalies)
    for spec in specs:
        alignment = align_signal(
            spec.signal,
            spec.kind,
            samples.get(spec.signal, []),
            grid,
            max_gap_periods=spec.max_gap_periods,
        )
        series.append(alignment)
        anomalies.extend(_series_anomalies(spec.signal, alignment))

    window_info = {
        "start_ts": float(start_ts),
        "end_ts": float(end_ts),
        "period_s": period_s,
    }
    run = AlignmentRun(
        run_id=run_id,
        grid=grid,
        window=window_info,
        cutting=cutting,
        series=series,
        anomalies=anomalies,
    )
    digest = canonical_digest(run_id, run.as_dict(full_precision=True))
    return AlignmentRun(
        run_id=run_id,
        grid=grid,
        window=window_info,
        cutting=cutting,
        series=series,
        anomalies=anomalies,
        digest=digest,
    )


def _point_from_dict(raw: dict) -> AlignedPoint:
    return AlignedPoint(
        index=int(raw["index"]),
        ts=float(raw["ts"]),
        value=None if raw.get("value") is None else float(raw["value"]),
        covered=bool(raw.get("covered", True)),
        trusted=bool(raw.get("trusted", True)),
        reasons=tuple(raw.get("reasons", ())),
        source_ts=None if raw.get("source_ts") is None else float(raw["source_ts"]),
    )


def run_from_dict(raw: dict) -> AlignmentRun:
    """从落盘产物字典重建对齐结果，供审计回放侧核对。

    重建只还原网格、炉段与逐点值，不重算 digest；digest 是否仍与内容一致
    交给 :func:`coke.alignment.replay.verify_alignment` 判定。
    """

    grid_raw = raw["grid"]
    grid = TimeGrid(
        origin=float(grid_raw["origin"]),
        period_s=float(grid_raw["period_s"]),
        count=int(grid_raw["count"]),
    )

    heats: list[Heat] = []
    for item in raw.get("heats", {}).get("heats", []):
        heats.append(
            Heat(
                heat_id=str(item["heat_id"]),
                oven=str(item["oven"]),
                seq=int(item["seq"]),
                start_ts=float(item["start_ts"]),
                end_ts=None if item.get("end_ts") is None else float(item["end_ts"]),
                start_action_ts=float(item["start_ts"]),
                end_action_ts=item.get("end_ts"),
                trusted=bool(item.get("trusted", True)),
                anomalies=tuple(item.get("anomalies", ())),
            )
        )
    cutting = HeatCutting(heats=heats, anomalies=raw.get("heats", {}).get("anomalies", []))

    series: list[SignalAlignment] = []
    for item in raw.get("series", []):
        series.append(
            SignalAlignment(
                signal=str(item["signal"]),
                kind=str(item["kind"]),
                policy=str(item["policy"]),
                points=[_point_from_dict(point) for point in item.get("points", [])],
                regressions=list(item.get("regressions", [])),
                trusted_points=int(item.get("trusted_points", 0)),
                untrusted_points=int(item.get("untrusted_points", 0)),
                uncovered_points=int(item.get("uncovered_points", 0)),
            )
        )

    return AlignmentRun(
        run_id=str(raw["run_id"]),
        grid=grid,
        window=dict(raw.get("window", {})),
        cutting=cutting,
        series=series,
        anomalies=list(raw.get("anomalies", [])),
        digest=str(raw.get("digest", "")),
    )
