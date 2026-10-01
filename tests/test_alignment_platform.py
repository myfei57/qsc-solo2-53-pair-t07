"""平台级趋势对齐、按炉回放与审计留存。"""

from __future__ import annotations

import copy

from coke.alignment.run import run_from_dict
from coke.alignment.replay import verify_alignment


def _run_two_heats(platform, clock):
    platform.start()
    platform.charge(oven="oven01")
    for _ in range(3):
        clock.advance(5)
        platform.cycle()
    clock.advance(5)
    platform.push(oven="oven01")

    platform.charge(oven="oven02")
    for _ in range(3):
        clock.advance(5)
        platform.cycle()
    clock.advance(5)
    platform.push(oven="oven02")


def test_alignment_run_cuts_heats_and_verifies(platform, clock):
    _run_two_heats(platform, clock)
    run = platform.build_alignment_run()
    heat_ids = [heat["heat_id"] for heat in run["heats"]["heats"]]
    assert heat_ids == ["oven01#1", "oven02#1"]
    jumps = platform.alignment_heats()
    assert jumps["boundary"] == "heat"
    assert {item["heat_id"] for item in jumps["heats"]} == set(heat_ids)

    replay = platform.replay_heat("oven01#1")
    assert replay["heat"]["oven"] == "oven01"
    assert replay["index_end"] >= replay["index_start"]
    assert replay["series"]
    assert replay["audit"]

    verification = platform.verify_alignment_run()
    assert verification["ok"] is True


def test_ingested_regression_is_marked_and_audited_as_degraded(platform, clock):
    platform.start()
    platform.charge(oven="oven01")
    start = clock.now()
    platform.ingest_signal("field_temp", start, 100.0, "analog")
    platform.ingest_signal("field_temp", start + 10.0, 102.0, "analog")
    # 倒退样本（校时重发）。
    platform.ingest_signal("field_temp", start + 5.0, 500.0, "analog")
    clock.advance(20)
    platform.push(oven="oven01")

    run = platform.build_alignment_run()
    reasons = {item["reason"] for item in run["anomalies"]}
    assert "timestamp_regression" in reasons
    series = next(item for item in run["series"] if item["signal"] == "field_temp")
    assert series["kind"] == "analog"
    assert series["policy"] == "last_hold"
    assert any("timestamp_regression" in point["reasons"] for point in series["points"])

    actions = [item["action"] for item in platform.audit.entries(limit=50)]
    assert "alignment_built" in actions
    built = next(item for item in platform.audit.entries(limit=50) if item["action"] == "alignment_built")
    assert built["severity"] == "warning"
    assert built["payload"]["degraded"] is True
    assert built["payload"]["digest"].startswith("sha256:")


def test_alignment_persists_across_restart(tmp_path, clock):
    from coke.app import build_platform
    from coke.config import default_config

    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    _run_two_heats(first, clock)
    run = first.build_alignment_run()
    run_id = run["run_id"]

    second = build_platform(default_config(), clock=clock, storage_root=storage)
    assert second.alignment_summary()["last_run"]["run_id"] == run_id
    verification = second.verify_alignment_run(run_id)
    assert verification["ok"] is True
    replay = second.replay_heat("oven02#1", run_id)
    assert replay["heat"]["heat_id"] == "oven02#1"


def test_tampered_journal_fails_verification(platform, clock):
    _run_two_heats(platform, clock)
    platform.build_alignment_run()
    recorded = platform.alignment_journal.read_all()
    tampered = copy.deepcopy(recorded[-1])
    tampered["series"][0]["points"][1]["value"] = -9999.0
    run = run_from_dict(tampered)
    result = verify_alignment(run, platform._audit_records())
    assert result.ok is False
    assert result.digest_match is False


def test_alignment_without_signals_raises(platform):
    # 清空已登记信号的场景：全新平台未登记任何信号时报错而不是产出空对齐。
    platform.signals._kinds.clear()
    platform.signals._samples.clear()
    try:
        platform.build_alignment_run()
    except Exception as error:  # noqa: BLE001 - 断言组件名即可
        assert getattr(error, "component") == "alignment"
    else:  # pragma: no cover - 必须抛出
        raise AssertionError("空信号对齐应当失败")


def test_status_exposes_alignment_summary(platform, clock):
    _run_two_heats(platform, clock)
    platform.build_alignment_run()
    summary = platform.status()["alignment"]
    assert summary["cutting_boundary"] == "heat"
    assert summary["grid_period_s"] == platform.config.alignment.grid_period_s
    assert summary["last_run"]["heat_count"] == 2
    assert {item["signal"] for item in summary["signals"]} == {
        "collector_pressure",
        "damper_angle",
        "dust_fan",
    }
