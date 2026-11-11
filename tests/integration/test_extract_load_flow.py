"""End-to-end data flow: real HTTP client -> local fake SODA -> validate -> SQLite."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fake_soda import FakeSoda, serve

from sla_watch import pipeline, warehouse
from sla_watch.config import Config
from sla_watch.soda import RetryPolicy, SodaClient, SodaError

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(
    tmp_path: Path, initial_batch: list[dict[str, Any]]
) -> Iterator[tuple[FakeSoda, str, Config]]:
    fake = FakeSoda(initial_batch)
    cfg = Config(db_path=tmp_path / "wh.db", landing_dir=tmp_path / "landing", page_size=3)
    with serve(fake) as url:
        yield fake, url, cfg


def client(url: str, cfg: Config) -> SodaClient:
    return SodaClient(
        base_url=url,
        page_size=cfg.page_size,
        sleep=lambda _s: None,
        rng=random.Random(0),
        retry=RetryPolicy(max_attempts=3),
    )


def test_initial_load_exact_records(env: tuple[FakeSoda, str, Config]) -> None:
    fake, url, cfg = env
    conn = warehouse.connect(cfg.db_path)
    res = pipeline.run_incremental(conn, client(url, cfg), cfg, "run1", NOW)

    assert (res.fetched, res.inserted, res.quarantined) == (10, 8, 2)
    assert warehouse.count(conn) == 8
    assert warehouse.count(conn, "quarantine") == 2
    row = conn.execute("SELECT * FROM raw_requests WHERE unique_key='63000002'").fetchone()
    assert row["agency"] == "NYPD"
    assert row["closed_date"] == "2026-11-01T01:05:00"
    assert row["borough"] == "QUEENS"
    assert row["channel"] == "ONLINE"
    reasons = {r["unique_key"]: r["reason"] for r in conn.execute("SELECT * FROM quarantine")}
    assert "agency" in reasons["63000007"]
    assert "created_date" in reasons["63000008"]
    # watermark = max loaded (:updated_at, :id), set in the same transaction
    assert warehouse.get_watermark(conn) == ("2026-11-01T10:00:05.000Z", "row-aab0")
    # paginated with page_size=3 -> 4 pages, keyset not offset
    assert len(fake.requests) == 4
    assert all("$offset" not in p for p in fake.requests)
    # landing batch + manifest written with checksum
    manifest = json.loads((cfg.landing_dir / f"{res.batch_id}.manifest.json").read_text())
    assert manifest["fetched"] == 10 and len(manifest["landing_sha256"]) == 64


def test_replaying_same_batch_creates_no_duplicates(env: tuple[FakeSoda, str, Config]) -> None:
    _, url, cfg = env
    conn = warehouse.connect(cfg.db_path)
    pipeline.run_incremental(conn, client(url, cfg), cfg, "run1", NOW)
    before = warehouse.content_checksum(conn)

    # Rerun: overlap window re-reads everything -> all unchanged.
    res = pipeline.run_incremental(conn, client(url, cfg), cfg, "run2", NOW)
    assert res.inserted == 0 and res.updated == 0
    assert res.unchanged == 8
    assert warehouse.count(conn) == 8
    assert warehouse.count(conn, "quarantine") == 2  # quarantine is idempotent too
    assert warehouse.content_checksum(conn) == before


def test_updates_change_state_without_duplicating_keys(
    env: tuple[FakeSoda, str, Config], update_batch: list[dict[str, Any]]
) -> None:
    fake, url, cfg = env
    conn = warehouse.connect(cfg.db_path)
    pipeline.run_incremental(conn, client(url, cfg), cfg, "run1", NOW)

    fake.rows = [
        r for r in fake.rows if r["unique_key"] not in {"63000001", "63000003", "63000009"}
    ]
    fake.rows += update_batch
    res = pipeline.run_incremental(conn, client(url, cfg), cfg, "run2", NOW)

    assert res.inserted == 1 and res.updated == 2
    assert warehouse.count(conn) == 9
    dupes = conn.execute(
        "SELECT unique_key FROM raw_requests GROUP BY unique_key HAVING COUNT(*) > 1"
    ).fetchall()
    assert dupes == []
    r1 = conn.execute(
        "SELECT status, closed_date FROM raw_requests WHERE unique_key='63000001'"
    ).fetchone()
    assert tuple(r1) == ("Closed", "2026-11-02T05:30:00")
    # Newer version of 63000009 was invalid -> quarantined, stored copy flagged stale.
    stale = conn.execute(
        "SELECT stale_quarantined FROM raw_requests WHERE unique_key='63000009'"
    ).fetchone()
    assert stale[0] == 1
    assert warehouse.get_watermark(conn) == ("2026-11-02T06:00:03.000Z", "row-aaa9")


def test_out_of_order_older_batch_changes_nothing(
    env: tuple[FakeSoda, str, Config],
    initial_batch: list[dict[str, Any]],
    update_batch: list[dict[str, Any]],
) -> None:
    fake, url, cfg = env
    fake.rows = [r for r in initial_batch if r["unique_key"] != "63000009"] + update_batch[:3]
    conn = warehouse.connect(cfg.db_path)
    pipeline.run_incremental(conn, client(url, cfg), cfg, "run1", NOW)
    after_new = warehouse.content_checksum(conn)

    # A stale replica serves the OLD versions; a backfill reads them all.
    fake.rows = initial_batch
    pipeline.run_backfill(conn, client(url, cfg), cfg, "run2", date(2026, 10, 1), date(2026, 12, 1))
    assert warehouse.content_checksum(conn) != ""
    r1 = conn.execute("SELECT status FROM raw_requests WHERE unique_key='63000001'").fetchone()
    assert r1[0] == "Closed"  # older version did not overwrite the newer one
    # Only the previously-missing key 63000009 was added.
    assert warehouse.count(conn) == 9
    assert after_new != warehouse.content_checksum(conn)


def test_crash_mid_run_then_rerun_recovers_exactly(
    env: tuple[FakeSoda, str, Config],
) -> None:
    fake, url, cfg = env
    conn = warehouse.connect(cfg.db_path)
    # page 1 succeeds, then the source fails non-retryably
    original_query = fake.query
    calls = {"n": 0}

    def flaky(params: dict[str, str]) -> list[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] == 1:
            fake.fail_queue.append((400, {}))
        return original_query(params)

    fake.query = flaky  # type: ignore[method-assign]
    with pytest.raises(SodaError):
        pipeline.run_incremental(conn, client(url, cfg), cfg, "run1", NOW)
    # first page committed atomically with its watermark; nothing partial beyond it
    assert warehouse.count(conn) == 3
    assert warehouse.get_watermark(conn) == ("2026-11-01T10:00:02.000Z", "row-aaa3")
    failed = conn.execute("SELECT status FROM batches").fetchone()[0]
    assert failed == "failed"

    fake.query = original_query  # type: ignore[method-assign]
    pipeline.run_incremental(conn, client(url, cfg), cfg, "run2", NOW)
    assert warehouse.count(conn) == 8
    assert warehouse.count(conn, "quarantine") == 2


def test_transient_errors_are_retried_in_the_real_flow(env: tuple[FakeSoda, str, Config]) -> None:
    fake, url, cfg = env
    fake.fail_queue = [(503, {}), (429, {"Retry-After": "0"})]
    conn = warehouse.connect(cfg.db_path)
    res = pipeline.run_incremental(conn, client(url, cfg), cfg, "run1", NOW)
    assert res.inserted == 8


def test_backfill_is_idempotent_and_does_not_move_watermark(
    env: tuple[FakeSoda, str, Config],
) -> None:
    _, url, cfg = env
    conn = warehouse.connect(cfg.db_path)
    pipeline.run_incremental(conn, client(url, cfg), cfg, "run0", NOW)
    wm = warehouse.get_watermark(conn)

    first = pipeline.run_backfill(
        conn, client(url, cfg), cfg, "run1", date(2026, 10, 1), date(2026, 12, 1)
    )
    c1 = warehouse.content_checksum(conn)
    assert len(first) == 2  # Oct + Nov slices
    # Completed slices are skipped (resumability) ...
    assert (
        pipeline.run_backfill(
            conn, client(url, cfg), cfg, "run2", date(2026, 10, 1), date(2026, 12, 1)
        )
        == []
    )
    # ... and a forced rerun yields an identical checksum.
    pipeline.run_backfill(
        conn, client(url, cfg), cfg, "run3", date(2026, 10, 1), date(2026, 12, 1), force=True
    )
    assert warehouse.content_checksum(conn) == c1
    assert warehouse.get_watermark(conn) == wm
    assert warehouse.count(conn) == 8


def test_window_filter_excludes_old_requests(env: tuple[FakeSoda, str, Config]) -> None:
    fake, url, cfg = env
    old = dict(fake.rows[1], unique_key="50000000", created_date="2026-01-01T00:00:00.000")
    old[":id"] = "row-old"
    fake.rows.append(old)
    conn = warehouse.connect(cfg.db_path)
    pipeline.run_incremental(conn, client(url, cfg), cfg, "run1", NOW)
    assert (
        conn.execute("SELECT COUNT(*) FROM raw_requests WHERE unique_key='50000000'").fetchone()[0]
        == 0
    )
