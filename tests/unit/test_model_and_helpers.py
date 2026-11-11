from __future__ import annotations

import io
import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from sla_watch import log, warehouse
from sla_watch.model import validate
from sla_watch.pipeline import month_slices, window_start

GOOD: dict[str, Any] = {
    ":id": "row-1",
    ":updated_at": "2026-11-01T10:00:00.000Z",
    "unique_key": "1",
    "created_date": "2026-11-01T09:00:00.000",
    "agency": "NYPD",
    "complaint_type": "Noise",
    "status": "Open",
    "latitude": "",
}


def test_valid_row_normalises_timestamps_and_blanks() -> None:
    rec, reason = validate(GOOD)
    assert reason is None and rec is not None
    assert rec.created_date == "2026-11-01T09:00:00"
    assert rec.latitude is None


def test_invalid_rows_report_reasons() -> None:
    rec, reason = validate({**GOOD, "agency": "", "unique_key": "abc"})
    assert rec is None
    assert reason is not None and "agency" in reason and "unique_key" in reason


def test_row_hash_ignores_system_fields_but_tracks_content() -> None:
    a, _ = validate(GOOD)
    b, _ = validate({**GOOD, ":updated_at": "2027-01-01T00:00:00.000Z"})
    c, _ = validate({**GOOD, "status": "Closed"})
    assert a and b and c
    assert a.row_hash() == b.row_hash() != c.row_hash()


def test_window_start_uses_new_york_local_date() -> None:
    # 03:30 UTC on Nov 3 is still Nov 2 in New York.
    now = datetime(2026, 11, 3, 3, 30, tzinfo=UTC)
    assert window_start(now, 7) == "2026-10-26T00:00:00"


def test_month_slices_half_open_and_cross_year() -> None:
    assert month_slices(date(2026, 11, 15), date(2027, 2, 3)) == [
        (date(2026, 11, 15), date(2026, 12, 1)),
        (date(2026, 12, 1), date(2027, 1, 1)),
        (date(2027, 1, 1), date(2027, 2, 1)),
        (date(2027, 2, 1), date(2027, 2, 3)),
    ]


def test_upsert_semantics_newer_wins_older_ignored() -> None:
    conn = warehouse.connect(":memory:")
    v1, _ = validate(GOOD)
    v2, _ = validate({**GOOD, ":updated_at": "2026-11-02T00:00:00.000Z", "status": "Closed"})
    assert v1 and v2
    conn.execute("BEGIN")
    assert warehouse.upsert(conn, [v1], "b1")["inserted"] == 1
    assert warehouse.upsert(conn, [v2], "b2")["updated"] == 1
    assert warehouse.upsert(conn, [v1], "b3")["unchanged"] == 1  # older version ignored
    conn.execute("COMMIT")
    status = conn.execute("SELECT status FROM raw_requests").fetchone()[0]
    assert status == "Closed"


def test_json_logs_redact_secrets() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(log.JsonFormatter())
    logger = logging.getLogger("redact-test")
    logger.handlers[:] = [handler]
    logger.setLevel("INFO")
    log.bind(run_id="r1")
    log.event(logger, "hello", app_token="secret", rows=3)
    line = json.loads(stream.getvalue())
    assert line["app_token"] == "***"
    assert line["rows"] == 3 and line["run_id"] == "r1" and line["msg"] == "hello"
