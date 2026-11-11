"""Socrata SODA client: keyset pagination on (:updated_at, :id) with bounded retries.

Offset pagination over a dataset that is being updated can skip or repeat rows, so
pages are fetched with a strict keyset predicate:
    (:updated_at > last_ts) OR (:updated_at = last_ts AND :id > last_id)
ordered by :updated_at, :id.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from sla_watch import log
from sla_watch.model import SOQL_SELECT

logger = log.get(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

Row = dict[str, Any]


class SodaError(RuntimeError):
    """Non-retryable error, or retries exhausted."""


@dataclass
class RetryPolicy:
    max_attempts: int = 6
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Full-jitter exponential backoff (attempt is 1-based)."""
        cap = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
        return rng.uniform(0, cap)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, dt.timestamp() - time.time())


@dataclass
class SodaClient:
    base_url: str = "https://data.cityofnewyork.us"
    dataset: str = "erm2-nwe9"
    app_token: str | None = None
    page_size: int = 50_000
    select: str = SOQL_SELECT
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    session: requests.Session = field(default_factory=requests.Session)
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    @property
    def url(self) -> str:
        return f"{self.base_url}/resource/{self.dataset}.json"

    def _get(self, params: dict[str, str]) -> list[Row]:
        headers = {"Accept": "application/json"}
        if self.app_token:
            headers["X-App-Token"] = self.app_token
        last_error = "unknown"
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                resp = self.session.get(
                    self.url,
                    params=params,
                    headers=headers,
                    timeout=(self.retry.connect_timeout_s, self.retry.read_timeout_s),
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                delay = self.retry.backoff(attempt, self.rng)
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, list):
                        raise SodaError(f"unexpected payload type {type(data).__name__}")
                    return data
                if resp.status_code not in RETRYABLE_STATUS:
                    raise SodaError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                last_error = f"HTTP {resp.status_code}"
                hinted = _retry_after_seconds(resp.headers.get("Retry-After"))
                delay = (
                    min(hinted, self.retry.max_delay_s)
                    if hinted is not None
                    else self.retry.backoff(attempt, self.rng)
                )
            if attempt == self.retry.max_attempts:
                break
            log.event(
                logger, "soda_retry", attempt=attempt, error=last_error, delay_s=round(delay, 2)
            )
            self.sleep(delay)
        raise SodaError(f"retries exhausted after {self.retry.max_attempts} attempts: {last_error}")

    def iter_pages(
        self,
        base_where: str,
        start_updated_at: str,
        start_id: str = "",
    ) -> Iterator[list[Row]]:
        """Yield pages of rows with :updated_at >= start (keyset-paginated)."""
        last_ts, last_id = start_updated_at, start_id
        first = True
        while True:
            if first and not last_id:
                key = f":updated_at >= '{last_ts}'"
            else:
                key = (
                    f"(:updated_at > '{last_ts}' OR "
                    f"(:updated_at = '{last_ts}' AND :id > '{last_id}'))"
                )
            first = False
            params = {
                "$select": self.select,
                "$where": f"({base_where}) AND {key}" if base_where else key,
                "$order": ":updated_at, :id",
                "$limit": str(self.page_size),
            }
            page = self._get(params)
            if not page:
                return
            yield page
            if len(page) < self.page_size:
                return
            last_ts, last_id = str(page[-1][":updated_at"]), str(page[-1][":id"])
