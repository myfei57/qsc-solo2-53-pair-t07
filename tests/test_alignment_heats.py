"""按炉切段（炉界定死，不按班次）。"""

from __future__ import annotations

from coke.alignment.heats import (
    BOUNDARY_REGRESSION,
    HEAT_OVERLAP,
    ORPHAN_END,
    UNTERMINATED,
    cut_heats,
    find_heat,
)
from coke.audit.record import AuditRecord


def _rec(ts: float, oven: str, action: str, ok: bool = True) -> AuditRecord:
    return AuditRecord.create(ts, oven, "audit", action, ok=ok)


def test_one_heat_bounded_by_charge_and_push():
    cutting = cut_heats(
        [_rec(0.0, "oven01", "charge_complete"), _rec(120.0, "oven01", "push_complete")]
    )
    assert len(cutting.heats) == 1
    heat = cutting.heats[0]
    assert heat.heat_id == "oven01#1"
    assert heat.start_ts == 0.0
    assert heat.end_ts == 120.0
    assert heat.trusted is True
    assert find_heat(cutting, "oven01#1") is heat
    assert find_heat(cutting, "oven01#9") is None
    assert cutting.as_dict()["boundary"] == "heat"


def test_parallel_ovens_do_not_overlap_counts():
    cutting = cut_heats(
        [
            _rec(0.0, "oven01", "charge_complete"),
            _rec(5.0, "oven02", "charge_complete"),
            _rec(50.0, "oven01", "push_complete"),
            _rec(60.0, "oven02", "push_complete"),
        ]
    )
    assert [heat.heat_id for heat in cutting.heats] == ["oven01#1", "oven02#1"]


def test_failed_operations_are_not_heat_boundaries():
    cutting = cut_heats(
        [
            _rec(0.0, "oven01", "charge_complete", ok=False),
            _rec(10.0, "oven01", "push_complete"),
        ]
    )
    # 装煤失败不算开始，推焦完成成为孤儿结束。
    assert cutting.heats == []
    assert cutting.anomalies[0]["reason"] == ORPHAN_END


def test_orphan_end_and_unterminated_are_partial_failures():
    cutting = cut_heats(
        [
            _rec(5.0, "oven02", "push_complete"),
            _rec(8.0, "oven03", "charge_complete"),
        ]
    )
    reasons = {item["reason"] for item in cutting.anomalies}
    assert ORPHAN_END in reasons
    assert UNTERMINATED in reasons
    unterminated = next(heat for heat in cutting.heats if heat.oven == "oven03")
    assert unterminated.end_ts is None
    assert UNTERMINATED in unterminated.anomalies


def test_arrival_order_preserves_boundary_regression():
    # 时间戳倒挂：装煤事件时间戳晚于推焦，但 journal 到达序是先装煤后推焦。
    cutting = cut_heats(
        [_rec(200.0, "oven04", "charge_complete"), _rec(150.0, "oven04", "push_complete")]
    )
    heat = cutting.heats[0]
    assert heat.trusted is False
    assert BOUNDARY_REGRESSION in heat.anomalies


def test_same_oven_overlapping_heats_are_flagged():
    cutting = cut_heats(
        [
            _rec(0.0, "oven01", "charge_complete"),
            _rec(10.0, "oven01", "charge_complete"),
            _rec(20.0, "oven01", "push_complete"),
            _rec(30.0, "oven01", "push_complete"),
        ]
    )
    second = find_heat(cutting, "oven01#2")
    assert second.trusted is False
    assert HEAT_OVERLAP in second.anomalies
