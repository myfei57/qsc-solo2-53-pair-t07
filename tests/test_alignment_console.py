"""对齐相关控制台接口。"""

from __future__ import annotations

from coke.console.api import Api


def _prepare(platform, clock):
    platform.start()
    platform.charge(oven="oven01")
    for _ in range(3):
        clock.advance(5)
        platform.cycle()
    clock.advance(5)
    platform.push(oven="oven01")


def test_alignment_endpoints_roundtrip(platform, clock):
    _prepare(platform, clock)
    api = Api(platform)

    status, run = api.handle("POST", "/api/alignment/run", {}, {})
    assert status == 200
    run_id = run["run_id"]
    assert run["heats"]["count"] == 1

    status, runs = api.handle("GET", "/api/alignment/runs", {}, {})
    assert status == 200
    assert runs["runs"][0]["run_id"] == run_id

    status, heats = api.handle("GET", "/api/alignment/heats", {}, {})
    assert status == 200
    assert heats["heats"][0]["heat_id"] == "oven01#1"

    status, replay = api.handle("GET", "/api/alignment/replay", {"heat_id": "oven01#1"}, {})
    assert status == 200
    assert replay["heat"]["oven"] == "oven01"

    status, verify = api.handle("GET", "/api/alignment/verify", {}, {})
    assert status == 200
    assert verify["ok"] is True


def test_signal_ingest_endpoint(platform, clock):
    api = Api(platform)
    status, payload = api.handle(
        "POST",
        "/api/signals/ingest",
        {},
        {"signal": "field_temp", "ts": 123.0, "value": 42.5, "kind": "analog"},
    )
    assert status == 200
    assert payload["sample"]["value"] == 42.5
    info = platform.signals.info("field_temp")
    assert info is not None and info.kind == "analog"


def test_replay_without_heat_id_is_409(platform, clock):
    _prepare(platform, clock)
    api = Api(platform)
    api.handle("POST", "/api/alignment/run", {}, {})
    status, payload = api.handle("GET", "/api/alignment/replay", {}, {})
    assert status == 409
    assert payload["component"] == "console"


def test_verify_before_any_alignment_is_409(platform):
    api = Api(platform)
    status, payload = api.handle("GET", "/api/alignment/verify", {}, {})
    assert status == 409
    assert payload["component"] == "alignment"
