"""align / replay 命令行。"""

from __future__ import annotations

import json

from coke.cli import main


def _write_tsv(path):
    # 两路信号：模拟量带一个超 3 周期缺口；状态量全程分段保持。
    lines = [
        "field_temp\t1758000000.0\t100.0\tanalog",
        "field_temp\t1758000010.0\t101.0\tanalog",
        "field_temp\t1758000040.0\t104.0\tanalog",
        "field_valve\t1758000000.0\t1.0\tstate",
        "field_valve\t1758000040.0\t0.0\tstate",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_align_then_replay_and_verify(tmp_path, capsys):
    storage = str(tmp_path / "state")
    tsv = tmp_path / "samples.tsv"
    _write_tsv(tsv)

    code = main(["--storage-root", storage, "align", "--tsv", str(tsv), "--brief"])
    assert code == 0
    run = json.loads(capsys.readouterr().out)
    assert run["grid"]["period_s"] == 5.0
    assert run["digest"].startswith("sha256:")
    assert {item["reason"] for item in run["anomalies"]} == {"sample_gap"}

    # 没有炉界（纯样本导入），跳转表为空但仍可核对一致性。
    code = main(["--storage-root", storage, "replay"])
    assert code == 0
    heats = json.loads(capsys.readouterr().out)
    assert heats["boundary"] == "heat"
    assert heats["heats"] == []

    code = main(["--storage-root", storage, "replay", "--verify"])
    assert code == 0
    verify = json.loads(capsys.readouterr().out)
    assert verify["ok"] is True
