"""Looker's symmetric aggregates, sum_distinct and window measures as unshackle writes them for Postgres,
on a real Postgres with the rows Looker computed its numbers from (tests/fixtures/looker_generated/)."""

from __future__ import annotations

from pathlib import Path

import lkml
import pytest
from harness import ENABLED, SKIP_REASON, Stack
from test_looker_generated import FIX, TILES, assert_lookers_numbers, evidence_tiles

pytestmark = [pytest.mark.live, pytest.mark.skipif(not ENABLED, reason=SKIP_REASON)]


def test_tile_sql_returns_lookers_numbers_on_postgres(tmp_path: Path):
    import psycopg

    tiles = evidence_tiles(tmp_path / "repo", "postgres")
    with Stack("warehouse") as stack, psycopg.connect(stack.dsn) as conn:
        for view in lkml.load((FIX / "evidence.view.lkml").read_text())["views"]:
            if view["name"] in ("ev_orders", "ev_items"):
                conn.execute(f"CREATE TABLE {view['name']} AS {view['derived_table']['sql'].replace('AS STRING', 'AS TEXT')}")
        for case in TILES:
            assert tiles[case].sql, tiles[case].todo
            cur = conn.execute(tiles[case].sql)
            assert_lookers_numbers(case, [c.name for c in cur.description], cur.fetchall())
