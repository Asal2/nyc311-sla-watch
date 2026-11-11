"""Command-line entry point: `sla-watch <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date

from sla_watch import log, pipeline, warehouse
from sla_watch.config import Config
from sla_watch.model import SOQL_SELECT
from sla_watch.soda import SodaClient


def _client(cfg: Config) -> SodaClient:
    return SodaClient(
        base_url=cfg.soda_base_url,
        dataset=cfg.soda_dataset,
        app_token=cfg.soda_app_token,
        page_size=cfg.page_size,
        select=SOQL_SELECT,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sla-watch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("extract-load", help="incremental extract + idempotent load")
    bf = sub.add_parser("backfill", help="re-extract a created_date range in month slices")
    bf.add_argument("--start", type=date.fromisoformat, required=True)
    bf.add_argument("--end", type=date.fromisoformat, required=True, help="exclusive")
    bf.add_argument("--force", action="store_true", help="redo slices already completed")
    sub.add_parser("status", help="print warehouse counts and watermark as JSON")
    args = parser.parse_args(argv)

    log.configure()
    cfg = Config.from_env()
    run_id = uuid.uuid4().hex[:12]
    log.bind(run_id=run_id, step=args.cmd)
    conn = warehouse.connect(cfg.db_path)

    if args.cmd == "extract-load":
        pipeline.run_incremental(conn, _client(cfg), cfg, run_id)
    elif args.cmd == "backfill":
        pipeline.run_backfill(conn, _client(cfg), cfg, run_id, args.start, args.end, args.force)
    elif args.cmd == "status":
        wm = warehouse.get_watermark(conn)
        out = {
            "rows": warehouse.count(conn),
            "quarantined": warehouse.count(conn, "quarantine"),
            "batches": warehouse.count(conn, "batches"),
            "watermark": wm[0] if wm else None,
            "checksum": warehouse.content_checksum(conn),
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
