"""对齐编排、按炉回放与一致性核对。"""

from __future__ import annotations

import copy

import pytest

from coke.alignment.grid import TimeGrid
from coke.alignment.replay import list_heat_jumps, replay_heat, verify_alignment
from coke.alignment.run import SignalSpec, build_alignment, canonical_digest, run_from_dict
from coke.alignment.signals import ANALOG, STATE, RawSample
from coke.audit.record import AuditRecord


def _rec(ts: float, oven: str, action: str, ok: bool = True) -> AuditRecord:
    return AuditRecord.create(ts, oven, "audit", action, ok=ok)


def _fixture():
    specs = [SignalSpec("pressure", ANALOG, 1), SignalSpec("valve", STATE, 1)]
    samples = {
        "pressure": [
            RawSample(0.0, 90.0),
            RawSample(5.0, 92.0),
            RawSample(10.0, 91.0),
            RawSample(15.0, 93.0),
            RawSample(20.0, 90.0),
        ],
        "valve": [RawSample(0.0, 1.0), RawSample(20.0, 0.0)],
    }
    records = [
        _rec(0.0, "oven01", "charge_complete"),
        _rec(20.0, "oven01", "push_complete"),
    ]
    return specs, samples, records


def test_build_alignment_uses_one_shared_grid_for_all_heats_and_signals():
    specs, samples, records = _fixture()
    run = build_alignment(specs, samples, records, 5.0, run_id="align-x")
    assert run.grid.timestamps() == [0.0, 5.0, 10.0, 15.0, 20.0]
    assert [item.signal for item in run.series] == ["pressure", "valve"]
    assert all(len(item.points) == run.grid.count for item in run.series)
    assert len(run.cutting.heats) == 1
    assert run.digest.startswith("sha256:")
    # digest 对全精度内容稳定。
    assert canonical_digest(run.run_id, run.as_dict(full_precision=True)) == run.digest


def test_replay_heat_slices_shared_axis_and_includes_heat_audit():
    specs, samples, records = _fixture()
    run = build_alignment(specs, samples, records, 5.0, run_id="align-x")
    jumps = list_heat_jumps(run)
    assert jumps[0]["heat_id"] == "oven01#1"
    assert jumps[0]["index_start"] == 0

    replay = replay_heat(run, "oven01#1", records)
    assert replay.index_start == 0
    assert replay.index_end == 4
    assert replay.grid_ts == run.grid.timestamps()
    assert {item["signal"] for item in replay.series} == {"pressure", "valve"}
    assert {entry["action"] for entry in replay.audit} == {"charge_complete", "push_complete"}
    assert replay.trusted is True


def test_replay_unknown_heat_raises():
    specs, samples, records = _fixture()
    run = build_alignment(specs, samples, records, 5.0, run_id="align-x")
    with pytest.raises(KeyError):
        replay_heat(run, "oven09#1", records)


def test_verify_passes_on_fresh_run_and_fails_on_tampering():
    specs, samples, records = _fixture()
    run = build_alignment(specs, samples, records, 5.0, run_id="align-x")
    result = verify_alignment(run, records)
    assert result.ok is True
    assert result.problems == []

    tampered = run_from_dict(copy.deepcopy(run.as_dict(full_precision=True)))
    object.__setattr__(tampered.series[0].points[2], "value", -1.0)
    bad = verify_alignment(tampered, records)
    assert bad.digest_match is False
    assert bad.ok is False


def test_roundtrip_through_full_precision_dict_preserves_digest():
    specs, samples, records = _fixture()
    run = build_alignment(specs, samples, records, 5.0, run_id="align-x")
    restored = run_from_dict(run.as_dict(full_precision=True))
    assert canonical_digest(restored.run_id, restored.as_dict(full_precision=True)) == run.digest
    assert verify_alignment(restored, records).ok is True


def test_regression_and_gap_surfaced_as_run_anomalies():
    specs = [SignalSpec("pressure", ANALOG, 1)]
    samples = {
        "pressure": [
            RawSample(0.0, 1.0),
            RawSample(10.0, 2.0),
            RawSample(5.0, 9.0),  # 倒退
            RawSample(20.0, 3.0),  # 10->20 缺口超过 1 周期
        ]
    }
    run = build_alignment(specs, samples, [], 5.0, run_id="align-y")
    reasons = {item["reason"] for item in run.anomalies}
    assert "timestamp_regression" in reasons
    assert "sample_gap" in reasons


def test_window_without_samples_falls_back_to_audit():
    specs = [SignalSpec("pressure", ANALOG, 1)]
    records = [_rec(3.0, "oven01", "charge_complete"), _rec(8.0, "oven01", "push_complete")]
    run = build_alignment(specs, {"pressure": []}, records, 5.0, run_id="align-z")
    assert run.grid.origin == 3.0
    assert run.grid.count == 2
    assert all(not point.covered for item in run.series for point in item.points)


def test_window_covers_heat_boundaries_earlier_than_samples():
    # 炉界从 t=0 开始，样本却只在 t=100 之后：窗口必须罩住炉界，核对才一致。
    specs = [SignalSpec("pressure", ANALOG, 3)]
    samples = {"pressure": [RawSample(100.0, 1.0), RawSample(105.0, 1.1)]}
    records = [_rec(0.0, "oven01", "charge_complete"), _rec(50.0, "oven01", "push_complete")]
    run = build_alignment(specs, samples, records, 5.0, run_id="align-w")
    assert run.grid.origin == 0.0
    assert verify_alignment(run, records).ok is True
    replay = replay_heat(run, "oven01#1", records)
    assert replay.index_start == 0
    assert replay.series[0]["points"][0]["covered"] is False
    assert replay.series[0]["points"][-1]["covered"] is False


def test_build_without_signals_or_any_data_rejected():
    with pytest.raises(ValueError):
        build_alignment([], {}, [], 5.0, run_id="x")
    with pytest.raises(ValueError):
        build_alignment([SignalSpec("p", ANALOG)], {"p": []}, [], 5.0, run_id="x")


def test_verify_flags_boundary_regression():
    specs = [SignalSpec("pressure", ANALOG, 3)]
    samples = {"pressure": [RawSample(0.0, 1.0), RawSample(20.0, 2.0)]}
    # 到达序先装煤(ts=20)后推焦(ts=10)：边界倒退。
    records = [_rec(20.0, "oven01", "charge_complete"), _rec(10.0, "oven01", "push_complete")]
    run = build_alignment(specs, samples, records, 5.0, run_id="align-r")
    result = verify_alignment(run, records)
    assert result.boundary_order_ok is False
    assert result.boundaries_mapped is False
    assert result.ok is False


def test_grid_equality_helper_assumptions():
    grid = TimeGrid.build(0.0, 20.0, 5.0)
    assert grid.count == 5
