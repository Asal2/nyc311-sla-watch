from __future__ import annotations

import random
from typing import Any

import pytest
import requests

from sla_watch.soda import RetryPolicy, SodaClient, SodaError, _retry_after_seconds


class FakeResp:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = headers or {}
        self.text = str(body)

    def json(self) -> Any:
        return self._body


class ScriptedSession:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResp:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make(script: list[Any], **kw: Any) -> tuple[SodaClient, list[float], ScriptedSession]:
    sleeps: list[float] = []
    session = ScriptedSession(script)
    client = SodaClient(
        session=session,  # type: ignore[arg-type]
        sleep=sleeps.append,
        rng=random.Random(0),
        retry=RetryPolicy(max_attempts=4, base_delay_s=1, max_delay_s=30),
        **kw,
    )
    return client, sleeps, session


def test_retries_429_and_honours_retry_after() -> None:
    client, sleeps, _ = make(
        [FakeResp(429, headers={"Retry-After": "7"}), FakeResp(200, [{"a": 1}])]
    )
    assert client._get({}) == [{"a": 1}]
    assert sleeps == [7.0]


def test_retry_after_is_capped() -> None:
    client, sleeps, _ = make([FakeResp(503, headers={"Retry-After": "9999"}), FakeResp(200, [])])
    client._get({})
    assert sleeps == [30]


def test_retries_5xx_and_timeouts_with_backoff_then_succeeds() -> None:
    client, sleeps, session = make(
        [
            FakeResp(500),
            requests.Timeout("slow"),
            requests.ConnectionError("reset"),
            FakeResp(200, []),
        ]
    )
    assert client._get({}) == []
    assert len(sleeps) == 3
    assert all(0 <= s <= 30 for s in sleeps)
    assert len(session.calls) == 4


def test_non_retryable_4xx_fails_fast() -> None:
    client, sleeps, session = make([FakeResp(400, {"message": "bad soql"})])
    with pytest.raises(SodaError, match="HTTP 400"):
        client._get({})
    assert sleeps == []
    assert len(session.calls) == 1


def test_exhausted_retries_raise() -> None:
    client, sleeps, _ = make([FakeResp(503)] * 4)
    with pytest.raises(SodaError, match="retries exhausted after 4"):
        client._get({})
    assert len(sleeps) == 3  # no sleep after the final attempt


def test_timeouts_are_passed_and_token_sent_as_header() -> None:
    client, _, session = make([FakeResp(200, [])], app_token="secret")
    client._get({})
    call = session.calls[0]
    assert call["timeout"] == (10.0, 60.0)
    assert call["headers"]["X-App-Token"] == "secret"


def test_non_list_payload_rejected() -> None:
    client, _, _ = make([FakeResp(200, {"error": True})])
    with pytest.raises(SodaError, match="unexpected payload"):
        client._get({})


def test_backoff_is_bounded_and_grows() -> None:
    policy = RetryPolicy(base_delay_s=1, max_delay_s=8)
    rng = random.Random(1)
    caps = [min(8, 2 ** (a - 1)) for a in range(1, 7)]
    for attempt, cap in enumerate(caps, start=1):
        assert 0 <= policy.backoff(attempt, rng) <= cap


@pytest.mark.parametrize(("value", "expected"), [("5", 5.0), ("", None), (None, None), ("x", None)])
def test_retry_after_parsing(value: str | None, expected: float | None) -> None:
    assert _retry_after_seconds(value) == expected


def test_keyset_pagination_params() -> None:
    page1 = [{":updated_at": "2026-11-01T00:00:00.000Z", ":id": f"row-{i}"} for i in range(2)]
    page2 = [{":updated_at": "2026-11-01T00:00:01.000Z", ":id": "row-9"}]
    client, _, session = make([FakeResp(200, page1), FakeResp(200, page2)], page_size=2)
    pages = list(
        client.iter_pages("created_date >= '2026-08-01T00:00:00'", "2026-10-31T00:00:00.000Z")
    )
    assert [len(p) for p in pages] == [2, 1]
    first, second = (c["params"] for c in session.calls)
    assert first["$where"].endswith(":updated_at >= '2026-10-31T00:00:00.000Z'")
    assert first["$order"] == ":updated_at, :id"
    assert "$offset" not in first
    assert (
        ":updated_at > '2026-11-01T00:00:00.000Z' OR "
        "(:updated_at = '2026-11-01T00:00:00.000Z' AND :id > 'row-1')"
    ) in second["$where"]
