"""趋势对齐与按炉回放。

各信号原本各采各的、时间对不齐，本包把它们对齐到一根统一时间网格
（:mod:`coke.alignment.grid`），按炉界（不按班次）切成一炉段工况
（:mod:`coke.alignment.heats`），信号怎么落网格按类型定（最近点前插／分段
保持，见 :mod:`coke.alignment.signals`），再支持按炉回放跳转与同时间轴一致
性核对（:mod:`coke.alignment.replay`）。时间戳倒退与采样缺失都属部分失败：
数据照出、受影响网格点标不可信，不整段丢弃。
"""

from __future__ import annotations

from .grid import TimeGrid
from .heats import Heat, HeatCutting, cut_heats, find_heat
from .replay import HeatReplay, Verification, list_heat_jumps, replay_heat, verify_alignment
from .run import AlignmentRun, SignalSpec, build_alignment, canonical_digest, run_from_dict
from .signals import (
    ANALOG,
    STATE,
    AlignedPoint,
    RawSample,
    SignalAlignment,
    align_signal,
    policy_for_kind,
)

__all__ = [
    "TimeGrid",
    "Heat",
    "HeatCutting",
    "cut_heats",
    "find_heat",
    "HeatReplay",
    "Verification",
    "list_heat_jumps",
    "replay_heat",
    "verify_alignment",
    "AlignmentRun",
    "SignalSpec",
    "build_alignment",
    "canonical_digest",
    "run_from_dict",
    "ANALOG",
    "STATE",
    "AlignedPoint",
    "RawSample",
    "SignalAlignment",
    "align_signal",
    "policy_for_kind",
]
