"""命令行入口。

五个子命令覆盖日常使用：

``serve``   启动控制台 HTTP 服务，可选地在启动后探测接口；
``demo``    在本地跑一遍完整流程（启动 → 装煤 → 换向 → 推焦 → 熄焦 → 清扫 →
            控制周期 → 并发探测 → 行程标定），用于交付前的真实运行检查；
``status``  打印当前状态快照；
``config``  打印生效配置；
``align``   把登记的各路信号对齐到统一时间网格并按炉界切段，落审计流水；
``replay``  按炉号回放某一炉（``--verify`` 时改为核对对齐与审计的一致性）；
``reset``   清掉落盘的平台计数。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .app import Platform, build_platform
from .clock import ManualClock, SystemClock
from .config import config_to_mapping, default_config, load_config
from .console.server import ConsoleServer, probe_endpoints
from .errors import CokeError

DEFAULT_PROBE_PATHS = ("/api/status", "/api/readiness", "/api/topology", "/api/audit")


def _build(args: argparse.Namespace) -> Platform:
    config = load_config(args.config) if getattr(args, "config", None) else default_config()
    if getattr(args, "storage_root", None):
        return build_platform(config, storage_root=args.storage_root)
    return Platform(config=config)


def _dump(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _cmd_serve(args: argparse.Namespace) -> int:
    platform = _build(args)
    server = ConsoleServer(platform, host=args.host, port=args.port)
    base_url = server.start()
    payload: dict = {"listening": base_url, "routes": server.api.routes()}
    if args.probe:
        payload["probe"] = probe_endpoints(base_url, list(DEFAULT_PROBE_PATHS))
    _dump(payload)
    if args.duration > 0:
        try:
            time.sleep(args.duration)
        except KeyboardInterrupt:  # pragma: no cover - 交互路径
            pass
        server.stop()
        return 0
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:  # pragma: no cover - 交互路径
        server.stop()
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    clock = ManualClock() if args.fast else SystemClock()
    config_path = getattr(args, "config", None)
    config = load_config(config_path) if config_path else default_config()
    platform = build_platform(config, clock=clock, storage_root=getattr(args, "storage_root", None))
    # 演示从干净基线起步：先清掉上一轮落盘的平台计数。
    platform.reset_state()

    startup = platform.start()
    charge = platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    disturbance = platform.disturb_collector(delta_pa=-45.0)
    clock.advance(config.heating.reversal_interval_s)
    reversal = platform.reverse(oven="oven01", temperature_c=config.heating.temp_target_c)
    push = platform.push(oven="oven01", guide_target_mm=0.0, guide_measured_mm=0.0)
    quench = platform.quench(oven="oven01")
    cycles = []
    for _ in range(8):
        # 控制周期用 dt 推进仿真；可推进时钟下同步推进墙钟，否则各周期采样
        # 时间戳全部相同，会被对齐层正确地判成时间戳倒退。
        clock.advance(1.0)
        cycles.append(platform.cycle(dt=1.0))
    cleaning = platform.clean(oven="oven01")
    # 趋势各采各的，演示里把它们对齐到统一网格并按炉切一段，验证可回放可核对。
    alignment = platform.build_alignment_run()
    alignment_verify = platform.verify_alignment_run(alignment["run_id"])
    concurrency = platform.concurrency_probe(rounds=args.rounds)
    calibration = platform.calibrate_stroke(value_mm=3212.0)
    shutdown = platform.stop()

    # 演示自检用临时端口，避免和本机常驻服务抢 8080。
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    probe = probe_endpoints(base_url, list(DEFAULT_PROBE_PATHS))
    server.stop()

    report = {
        "startup": startup,
        "charge": {"ok": charge["ok"], "steps": [step["action"] for step in charge["steps"]]},
        "disturbance": disturbance,
        "reversal": {"ok": reversal["ok"], "steps": [step["action"] for step in reversal["steps"]]},
        "push": {"ok": push["ok"], "steps": [step["action"] for step in push["steps"]]},
        "quench": {"ok": quench["ok"], "steps": [step["action"] for step in quench["steps"]]},
        "cycles_run": len(cycles),
        "last_cycle": cycles[-1],
        "cleaning": {"ok": cleaning["ok"], "steps": [step["action"] for step in cleaning["steps"]]},
        "alignment": {
            "run_id": alignment["run_id"],
            "heat_count": alignment["heats"]["count"],
            "grid_points": alignment["grid"]["count"],
            "anomalies": len(alignment["anomalies"]),
            "verified": alignment_verify["ok"],
        },
        "concurrency": concurrency,
        "calibration": calibration,
        "shutdown": shutdown,
        "probe": probe,
        "audit": platform.audit.summary(),
    }
    _dump(report)
    failures = [name for name, item in probe.items() if not item["ok"]]
    for operation in (charge, reversal, push, quench, cleaning):
        if not operation["ok"]:
            failures.extend(operation["violations"])
    if failures:
        print(f"检查未通过: {failures}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    platform = _build(args)
    payload = platform.status()
    payload["topology"] = platform.topology_summary()
    payload["namespaces"] = platform.namespaces_describe()
    payload["trend"] = platform.trend.snapshot()
    _dump(payload)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    config_path = getattr(args, "config", None)
    config = load_config(config_path) if config_path else default_config()
    _dump(config_to_mapping(config))
    return 0


def _cmd_align(args: argparse.Namespace) -> int:
    platform = _build(args)
    if args.tsv:
        count = _ingest_tsv(platform, args.tsv)
        print(f"已收录 {count} 条原始样本", file=sys.stderr)
    payload = platform.build_alignment_run()
    if args.brief:
        _dump(
            {
                "run_id": payload["run_id"],
                "window": payload["window"],
                "grid": payload["grid"],
                "heats": [
                    {key: heat[key] for key in ("heat_id", "start_ts", "end_ts", "trusted", "anomalies")}
                    for heat in payload["heats"]["heats"]
                ],
                "anomalies": payload["anomalies"],
                "digest": payload["digest"],
            }
        )
    else:
        _dump(payload)
    return 0


def _ingest_tsv(platform: Platform, path: str) -> int:
    """从 TSV（signal<TAB>ts<TAB>value，可选第四列 kind）按行收录原始样本。"""

    count = 0
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                raise CokeError(f"{path}:{line_number} 行列数不足（需 signal/ts/value）", component="cli")
            kind = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
            platform.ingest_signal(parts[0].strip(), float(parts[1]), float(parts[2]), kind)
            count += 1
    return count


def _cmd_replay(args: argparse.Namespace) -> int:
    platform = _build(args)
    if args.verify:
        report = platform.verify_alignment_run(run_id=args.run_id)
        _dump(report)
        return 0 if report["ok"] else 1
    if not args.heat:
        _dump(platform.alignment_heats(run_id=args.run_id))
        return 0
    _dump(platform.replay_heat(heat_id=args.heat, run_id=args.run_id))
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    platform = _build(args)
    removed = platform.reset_state()
    _dump({"storage_root": platform.store.root, "platform_state_removed": removed})
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="JSON 配置文件路径", default=argparse.SUPPRESS)
    common.add_argument("--storage-root", help="覆盖状态存储目录", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(
        prog="coke", description="工业顺序控制与联锁服务", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动控制台服务", parents=[common])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--duration", type=float, default=0.0, help="运行多少秒后退出，0 表示常驻")
    serve.add_argument("--probe", action="store_true", help="启动后探测接口")
    serve.set_defaults(func=_cmd_serve)

    demo = sub.add_parser("demo", help="跑一遍完整流程", parents=[common])
    demo.add_argument("--ovens", type=int, default=1, help="演示的碳化室数量（保留兼容）")
    demo.add_argument("--rounds", type=int, default=24, help="并发探测轮数")
    demo.add_argument("--fast", action="store_true", help="使用可推进时钟，跳过真实等待")
    demo.set_defaults(func=_cmd_demo)

    status = sub.add_parser("status", help="打印状态快照", parents=[common])
    status.set_defaults(func=_cmd_status)

    config = sub.add_parser("config", help="打印生效配置", parents=[common])
    config.set_defaults(func=_cmd_config)

    align = sub.add_parser("align", help="对齐统一时间网格并按炉切段", parents=[common])
    align.add_argument("--tsv", help="从 TSV（signal ts value [kind]）导入现场原始样本")
    align.add_argument("--brief", action="store_true", help="只打印窗口、炉段与异常，省略逐点值")
    align.set_defaults(func=_cmd_align)

    replay = sub.add_parser("replay", help="列出炉段或按炉回放", parents=[common])
    replay.add_argument("--heat", help="炉号（形如 oven01#1）；缺省只列炉段跳转表")
    replay.add_argument("--run-id", dest="run_id", help="指定对齐结果，缺省取最近一次")
    replay.add_argument("--verify", action="store_true", help="改为核对对齐与审计的一致性")
    replay.set_defaults(func=_cmd_replay)

    reset = sub.add_parser("reset", help="清掉落盘的平台计数", parents=[common])
    reset.set_defaults(func=_cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CokeError as error:
        print(json.dumps(error.as_record(), ensure_ascii=False), file=sys.stderr)
        return 2
