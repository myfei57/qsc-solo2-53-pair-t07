"""统一时间网格。"""

from __future__ import annotations

import pytest

from coke.alignment.grid import TimeGrid


def test_grid_is_anchored_at_origin_and_closed_at_end():
    grid = TimeGrid.build(100.0, 120.0, 5.0)
    assert grid.origin == 100.0
    assert grid.count == 5
    assert grid.timestamps() == [100.0, 105.0, 110.0, 115.0, 120.0]
    assert grid.as_dict()["end_ts"] == 120.0


def test_grid_degenerate_window_keeps_anchor_only():
    grid = TimeGrid.build(10.0, 10.0, 5.0)
    assert grid.count == 1
    assert grid.ts_at(0) == 10.0


def test_grid_rejects_reversed_window_and_nonpositive_period():
    with pytest.raises(ValueError):
        TimeGrid.build(10.0, 9.0, 5.0)
    with pytest.raises(ValueError):
        TimeGrid.build(0.0, 10.0, 0.0)


def test_grid_index_and_cover_indices_clamp():
    grid = TimeGrid.build(0.0, 20.0, 5.0)
    assert grid.index_of(12.3) == 2
    assert grid.clamp_index(-100.0) == 0
    assert grid.clamp_index(100.0) == 4
    assert grid.cover_indices(7.0, 13.0) == (1, 2)
    assert grid.cover_indices(-5.0, 999.0) == (0, 4)
