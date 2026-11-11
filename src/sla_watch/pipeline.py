"""Extract-load orchestration: incremental runs and month-sliced backfills."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sla_watch import log, warehouse
from sla_watch.config import Config
from sla_watch.model import validate
from sla_watch.soda import Row, SodaClient

logger = log.get(__name__)
NY = ZoneInfo("America/New_York")
EPOCH = "1970-01-01T00:00:00.000Z"


@dataclass
class BatchResult:
    batch_id: str
    kind: str
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    quarantined: int = 0
    watermark_before: str | None = None
    watermark_after: str | None = None
    landing_sha256: str | None = None


def window_start(now_utc: datetime, window_days: int) -> str:
    """Start of the rolling window as a NY-local floating timestamp (midnight)."""
    local_day = now_utc.astimezone(NY).date() - timedelta(days=window_days)
    return f"{local_day.isoformat()}T00:00:00"


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _begin_batch(conn: sqlite3.Connection, run_id: str, kind: str, wm_before: str | None) -> str:
    batch_id = f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO batches(batch_id, run_id, kind, started_at, watermark_before) "
        "VALUES (?, ?, ?, ?, ?)",
        (batch_id, run_id, kind, datetime.now(UTC).isoformat(timespec="seconds"), wm_before),
    )
    return batch_id


def _process_page(
    conn: sqlite3.Connection, page: list[Row], result: BatchResult, move_watermark: bool
) -> None:
    """Validate + upsert one page and (optionally) advance the watermark atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        valid = []
        for raw in page:
            rec, reason = validate(raw)
            if rec is None:
                warehouse.quarantine(conn, result.batch_id, raw, reason or "invalid")
                result.quarantined += 1
            else:
                valid.append(rec)
        counts = warehouse.upsert(conn, valid, result.batch_id)
        result.fetched += len(page)
        result.inserted += counts["inserted"]
        result.updated += counts["updated"]
        result.unchanged += counts["unchanged"]
        last = page[-1]
        if move_watermark:
            warehouse.set_watermark(conn, str(last[":updated_at"]), str(last[":id"]))
            result.watermark_after = str(last[":updated_at"])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _finish_batch(conn: sqlite3.Connection, r: BatchResult, status: str) -> None:
    conn.execute(
        "UPDATE batches SET finished_at=?, rows_fetched=?, rows_inserted=?, rows_updated=?, "
        "rows_unchanged=?, rows_quarantined=?, watermark_after=?, status=? WHERE batch_id=?",
        (
            datetime.now(UTC).isoformat(timespec="seconds"),
            r.fetched,
            r.inserted,
            r.updated,
            r.unchanged,
            r.quarantined,
            r.watermark_after,
            status,
            r.batch_id,
        ),
    )


def _run_batch(
    conn: sqlite3.Connection,
    pages: Callable[[], Iterable[list[Row]]],
    result: BatchResult,
    move_watermark: bool,
    landing_dir: Path | None,
) -> BatchResult:
    sink = None
    digest = hashlib.sha256()
    if landing_dir is not None:
        landing_dir.mkdir(parents=True, exist_ok=True)
        sink = (landing_dir / f"{result.batch_id}.jsonl").open("w", encoding="utf-8")
    try:
        for page in pages():
            if sink is not None:
                for raw in page:
                    line = json.dumps(raw, sort_keys=True) + "\n"
                    sink.write(line)
                    digest.update(line.encode())
            _process_page(conn, page, result, move_watermark)
    except BaseException:
        _finish_batch(conn, result, "failed")
        raise
    finally:
        if sink is not None:
            sink.close()
    result.landing_sha256 = digest.hexdigest() if sink is not None else None
    _finish_batch(conn, result, "succeeded")
    if landing_dir is not None:
        manifest = {**result.__dict__}
        (landing_dir / f"{result.batch_id}.manifest.json").write_text(
            json.dumps(manifest, indent=2)
        )
    log.event(logger, "batch_done", **result.__dict__)
    return result


def run_incremental(
    conn: sqlite3.Connection,
    client: SodaClient,
    cfg: Config,
    run_id: str,
    now_utc: datetime | None = None,
) -> BatchResult:
    now_utc = now_utc or datetime.now(UTC)
    wm = warehouse.get_watermark(conn)
    if wm is None:
        start_ts = EPOCH
    else:
        start_ts = _fmt_utc(_parse_utc(wm[0]) - timedelta(hours=cfg.overlap_hours))
    base_where = f"created_date >= '{window_start(now_utc, cfg.window_days)}'"
    batch_id = _begin_batch(conn, run_id, "incremental", wm[0] if wm else None)
    result = BatchResult(
        batch_id=batch_id, kind="incremental", watermark_before=wm[0] if wm else None
    )
    result.watermark_after = result.watermark_before
    return _run_batch(
        conn,
        lambda: client.iter_pages(base_where, start_ts),
        result,
        move_watermark=True,
        landing_dir=cfg.landing_dir,
    )


def month_slices(start: date, end: date) -> list[tuple[date, date]]:
    """Half-open [a, b) month slices covering [start, end)."""
    slices: list[tuple[date, date]] = []
    cur = start
    while cur < end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        slices.append((cur, min(nxt, end)))
        cur = nxt
    return slices


def run_backfill(
    conn: sqlite3.Connection,
    client: SodaClient,
    cfg: Config,
    run_id: str,
    start: date,
    end: date,
    force: bool = False,
) -> list[BatchResult]:
    """Re-extract by created_date range in month slices. Never moves the watermark.
    Completed slices are recorded so a failed backfill resumes where it stopped."""
    results = []
    for a, b in month_slices(start, end):
        key = f"backfill_done:{a.isoformat()}:{b.isoformat()}"
        if not force and warehouse.get_state(conn, key):
            log.event(logger, "backfill_slice_skip", slice_start=str(a), slice_end=str(b))
            continue
        where = (
            f"created_date >= '{a.isoformat()}T00:00:00' "
            f"AND created_date < '{b.isoformat()}T00:00:00'"
        )
        batch_id = _begin_batch(conn, run_id, "backfill", None)
        res = _run_batch(
            conn,
            lambda w=where: client.iter_pages(w, EPOCH),  # type: ignore[misc]
            BatchResult(batch_id=batch_id, kind="backfill"),
            move_watermark=False,
            landing_dir=cfg.landing_dir,
        )
        warehouse.set_state(conn, key, res.batch_id)
        results.append(res)
    return results
