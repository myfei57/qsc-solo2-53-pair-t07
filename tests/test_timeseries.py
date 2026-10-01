"""统一时间网格、炉段切段与回放校验。"""

from __future__ import annotations

from coke.clock import ManualClock
from coke.config import default_config
from coke.app import build_platform
from coke.timeseries import SignalPolicy, TrendBuffer
from coke.timeseries.grid import (
    BAD_CLOCK_ROLLBACK,
    BAD_MISSING,
    HELD_PREVIOUS,
    INTERPOLATED_LINEAR,
    OBSERVED,
)


def test_analog_signal_is_linearly_aligned_without_crossing_heat_boundary():
    buffer = TrendBuffer(period_s=5.0)
    for ts, value in ((0.0, 10.0), (5.0, 20.0), (10.0, 30.0), (25.0, 60.0)):
        buffer.append("analog", ts, value)
    points = buffer.aligned("analog", 0.0, 30.0)
    assert [(point.ts, point.quality) for point in points] == [
        (0.0, OBSERVED),
        (5.0, OBSERVED),
        (10.0, OBSERVED),
        (15.0, BAD_MISSING),
        (20.0, BAD_MISSING),
        (25.0, OBSERVED),
    ]
    assert points[0].value == 10.0

    interpolated = TrendBuffer(period_s=5.0)
    interpolated.append("analog", 0.0, 10.0)
    interpolated.append("analog", 10.0, 20.0)
    point = interpolated.aligned("analog", 0.0, 15.0)[1]
    assert point.quality == INTERPOLATED_LINEAR
    assert point.value == 15.0


def test_state_signal_holds_previous_value_until_max_age():
    buffer = TrendBuffer(period_s=5.0)
    buffer.register("valve", SignalPolicy.state(max_age_s=11.0))
    buffer.append("valve", 1.0, 1.0)
    points = buffer.aligned("valve", 0.0, 20.0)
    assert [point.quality for point in points] == [
        "bad_boundary",
        HELD_PREVIOUS,
        HELD_PREVIOUS,
        "bad_stale",
    ]
    assert [point.value for point in points[1:3]] == [1.0, 1.0]


def test_timestamp_rollback_quarantines_sample_and_marks_grid_untrusted():
    buffer = TrendBuffer(period_s=5.0)
    buffer.append("analog", 0.0, 10.0)
    buffer.append("analog", 10.0, 20.0)
    issue = buffer.append("analog", 3.0, 99.0)
    assert issue is not None
    assert issue.high_water_ts == 10.0
    points = buffer.aligned("analog", 0.0, 20.0)
    assert points[0].quality == OBSERVED
    assert points[1].quality == BAD_CLOCK_ROLLBACK
    assert points[2].quality == OBSERVED
    assert all(point.value != 99.0 for point in points)
    assert len(buffer.clock_issues("analog")) == 1


def test_heat_segments_use_heat_boundaries_not_shift_boundaries(platform):
    platform.start()
    platform.charge(oven="oven01")
    clock: ManualClock = platform.clock
    clock.advance(10)
    platform.quench(oven="oven01")
    clock.advance(10)
    platform.charge(oven="oven02")

    payload = platform.heat_segments()
    assert payload["segment_basis"] == "heat"
    assert [segment["oven"] for segment in payload["segments"]] == ["oven01", "oven02"]
    first, second = payload["segments"]
    assert first["open"] is False
    assert first["end_ts"] > first["start_ts"]
    assert second["open"] is True
    assert first["end_ts"] < second["start_ts"]


def test_heat_replay_persists_and_verifies_same_time_axis(platform):
    platform.start()
    platform.charge(oven="oven01")
    clock: ManualClock = platform.clock
    for offset in range(0, 21, 5):
        platform.trend.append("collector_pressure", clock.now() + offset, 90.0 + offset)
    clock.advance(20)
    platform.quench(oven="oven01")

    heat_id = platform.heat_segments()["segments"][0]["heat_id"]
    manifest = platform.build_heat_replay(heat_id)
    assert manifest["segment_basis"] == "heat"
    assert manifest["time_axis"]["unit"] == "unix_seconds"
    assert manifest["time_axis"]["time_zone"] == "utc"
    assert manifest["time_axis"]["grid_period_s"] == platform.trend.period_s
    assert manifest["quality"]["trusted"] is True
    assert manifest["checksum"]

    verification = platform.verify_heat_replay(heat_id)
    assert verification["ok"] is True
    tampered = dict(manifest)
    tampered["time_axis"] = {**manifest["time_axis"], "grid_period_s": 3.0}
    assert platform.heat_replay.verify(tampered)["ok"] is False

    before = platform.audit.summary()["journal_count"]
    loaded = platform.replay_heat(heat_id)
    assert loaded["checksum"] == manifest["checksum"]
    assert platform.audit.summary()["journal_count"] == before


def test_partial_sample_failure_marks_heat_untrusted_but_keeps_replay(platform):
    platform.start()
    platform.charge(oven="oven01")
    clock: ManualClock = platform.clock
    platform.trend.append("collector_pressure", clock.now(), 90.0)
    clock.advance(10)
    platform.trend.append("collector_pressure", clock.now(), 95.0)
    platform.quench(oven="oven01")

    heat_id = platform.heat_segments()["segments"][0]["heat_id"]
    manifest = platform.build_heat_replay(heat_id)
    assert manifest["quality"]["partial_failure"] is True
    assert manifest["quality"]["trusted"] is False
    assert "missing_or_untrusted_samples" in manifest["warnings"]
    assert manifest["quality"]["bad_points"] >= 1
    assert platform.verify_heat_replay(heat_id)["ok"] is True


def test_console_exposes_heat_list_replay_and_verification(platform):
    from coke.console.api import Api

    api = Api(platform)
    platform.start()
    platform.charge(oven="oven01")
    platform.clock.advance(10)
    platform.quench(oven="oven01")

    status, heats = api.handle("GET", "/api/heats", {}, {})
    assert status == 200
    heat_id = heats["segments"][0]["heat_id"]

    status, built = api.handle("POST", "/api/heats/replay", {}, {"heat_id": heat_id})
    assert status == 200
    assert built["heat"]["heat_id"] == heat_id

    status, replay = api.handle("GET", "/api/heats/replay", {"heat_id": heat_id}, {})
    assert status == 200
    assert replay["checksum"] == built["checksum"]

    status, verification = api.handle("GET", "/api/heats/verify", {"heat_id": heat_id}, {})
    assert status == 200
    assert verification["ok"] is True


def test_platform_can_be_built_with_isolated_storage(tmp_path):
    clock = ManualClock()
    platform = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))
    assert platform.heat_replay is not None
    assert platform.heat_segments()["segments"] == []
