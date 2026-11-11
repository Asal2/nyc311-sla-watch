"""A local HTTP server that mimics the SODA endpoint closely enough to exercise the
real client end to end: SoQL where-filters used by the pipeline, ordering by
(:updated_at, :id), $limit, and injectable transient failures."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

_CREATED_GE = re.compile(r"created_date >= '([^']+)'")
_CREATED_LT = re.compile(r"created_date < '([^']+)'")
_UPD_GE = re.compile(r":updated_at >= '([^']+)'")
_KEYSET = re.compile(r":updated_at > '([^']+)' OR \(:updated_at = '([^']+)' AND :id > '([^']+)'\)")


def _norm_created(value: str) -> str:
    return value[:19]


class FakeSoda:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.fail_queue: list[tuple[int, dict[str, str]]] = []
        self.requests: list[dict[str, str]] = []

    def query(self, params: dict[str, str]) -> list[dict[str, Any]]:
        where = params.get("$where", "")
        rows = self.rows
        if m := _CREATED_GE.search(where):
            rows = [
                r for r in rows if _norm_created(str(r.get("created_date", ""))) >= m.group(1)[:19]
            ]
        if m := _CREATED_LT.search(where):
            rows = [
                r for r in rows if _norm_created(str(r.get("created_date", ""))) < m.group(1)[:19]
            ]
        if m := _KEYSET.search(where):
            ts, _, last_id = m.groups()
            rows = [
                r
                for r in rows
                if r[":updated_at"] > ts or (r[":updated_at"] == ts and r[":id"] > last_id)
            ]
        elif m := _UPD_GE.search(where):
            rows = [r for r in rows if r[":updated_at"] >= m.group(1)]
        rows = sorted(rows, key=lambda r: (r[":updated_at"], r[":id"]))
        return rows[: int(params.get("$limit", "1000"))]


@contextmanager
def serve(fake: FakeSoda) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            fake.requests.append(params)
            if fake.fail_queue:
                status, headers = fake.fail_queue.pop(0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b'{"error": "injected"}')
                return
            body = json.dumps(fake.query(params)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
