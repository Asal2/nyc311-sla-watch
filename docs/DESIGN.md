# NYC 311 SLA Watch — Design (v2, revised after review)

v1 was reviewed by two independent agents (reliability/correctness; complexity/cost).
Every accepted or rejected point is recorded in [DECISIONS.md](DECISIONS.md).

## 1. Problem and users

NYC 311 logs ~3M+ service requests a year. NYC Open Data publishes every record, but
there is no simple, daily-refreshed answer to: *"Which agencies and complaint types
are falling behind on closing requests in my area, compared with their normal pace?"*

- **Community board district managers / council constituent-services staff** — a
  weekly list of complaint types in their district whose closure rate has degraded,
  to escalate with the responsible agency.
- **Local reporters / civic analysts** — a trustworthy, reproducible CSV of closure
  performance by agency × complaint type × borough × community board.

## 2. Data source

NYC Open Data "311 Service Requests from 2010 to Present" (`erm2-nwe9`), Socrata SODA
API. Free; a free app token is used in CI because Actions runners share IPs.

- Records mutate after creation, so extraction keys on the system fields
  `(:updated_at, :id)` with **keyset pagination** (never `$offset`).
- `created_date` / `closed_date` are **floating New York local time**; `:updated_at`
  is UTC. Local times are converted with `zoneinfo("America/New_York")`; durations
  are computed in UTC; window and week boundaries are defined in NY local time.
- Scope: requests **created in the last 90 days** (configurable; 7 days for dev), and
  ~16 needed columns only.

## 3. Metric definition (bias-aware)

Median time-to-close over recent requests is biased: only the fast ones have closed
yet. The headline metric is therefore **"% closed within N days"** (N = 3, 7, 14),
computed per *created-week cohort* only once the cohort is at least N days old, with
open counts reported separately.

Rows get a `close_quality` flag in `fct_requests` and non-`ok` rows are excluded from
metrics: `ok`, `sentinel_date` (≤ 1901), `future_close`, `close_before_create`,
`closed_no_date`, `instant_close` (< 60s; typically duplicates). Degradation is only
flagged with a minimum cohort size (default 30). `community_board` values such as
`0 Unspecified` are grouped as `Unspecified`.

## 4. Deliverables

1. Python package `sla_watch` (stdlib `sqlite3` + `requests` + `pydantic`; no pandas)
   with CLI: `extract-load`, `transform`, `check`, `report`, `run`, `backfill`,
   `reconcile-deletes`.
2. SQLite warehouse: `raw_requests`, `pipeline_state`, `batches`, `quarantine`,
   `fct_requests`, `mart_closure_weekly`, `mart_degradation`.
3. Data-quality suite (warn/fail); fail blocks publish (write-audit-publish).
4. Static report (HTML + CSV + `status.json` with `last_updated`, source lag and
   pipeline lag) on GitHub Pages.
5. Failure investigator: deterministic evidence bundle + rule-based hypotheses →
   one GitHub issue per failing check (comments on repeats); optional LLM summary.
6. CI (ruff, mypy, unit, integration) + scheduled pipeline + backfill workflows.

## 5. Milestones

- **M0** Design, decisions, skeleton, CI green.
- **M1** Extract (keyset, retrying) + idempotent load + state table; integration tests.
- **M2** Transforms (full rebuild), close-quality flags, cohort metrics, DQ suite, quarantine.
- **M3** Report + Pages + scheduled workflow + release-asset state + backfill workflow.
- **M4** Investigator (evidence bundle, rule-based hypotheses, idempotent issues, optional LLM).
- **M5** Delete reconciliation, README, diagram, example output, fresh-checkout verification.

Progress and the next action live in [STATUS.md](STATUS.md) so any session can resume.

## 6. Completion criteria

1. Fresh clone: `make setup && make test` passes with **no network** (fixtures).
2. Integration: a known batch yields exact rows/values; replaying it adds 0 rows and
   leaves the content checksum (excluding run ids/ingest timestamps) unchanged; a later
   batch with updated records changes state without duplicating keys; an out-of-order
   (older) batch changes nothing.
3. Backfill over a fixture range run twice → identical checksums; backfill does not
   move the incremental watermark.
4. Injected bad batch → `check` fails, report not republished (old report gets a
   staleness banner), evidence bundle written, investigator produces an issue body.
5. Retries unit-tested: 429 + `Retry-After`, 5xx, timeouts, non-retryable 4xx.
6. Live scheduled pipeline succeeds 3 consecutive days; `status.json` pipeline lag
   < 26h (source lag reported separately and not counted against the pipeline).
7. CI green on `main`; README verified from a fresh checkout.

## 7. Architecture

```
          ┌──────────── GitHub Actions (cron 06:17 UTC, workflow_dispatch backfill) ───────────┐
SODA API ─┼▶ extract (keyset (:updated_at,:id), retries) ─▶ batch JSONL + manifest (artifact) │
          │        │                                                                          │
          │        ▼  validate (pydantic) ── bad rows ──▶ quarantine                          │
          │  SQLite raw_requests  ◀── UPSERT newer-wins + row hash; watermark in SAME txn     │
          │        │                                                                          │
          │        ▼  transform: full rebuild in one txn                                      │
          │  fct_requests (close_quality, UTC durations) ─▶ mart_closure_weekly ─▶ degradation│
          │        │                                                                          │
          │        ▼  check (completeness, schema drift, freshness, volume, nulls, quality)   │
          │   pass │                          fail │                                          │
          │        ▼                               ▼                                          │
          │   report → Pages              evidence bundle → investigator → GitHub issue       │
          │        │                                                                          │
          │        ▼  state-<run_id>.db.gz + .sha256 → release "state" (keep 7)              │
          └───────────────────────────────────────────────────────────────────────────────────┘
```

### Failure handling

- **HTTP:** connect 10s / read 60s timeouts; full-jitter exponential backoff; honours
  `Retry-After`; 6 attempts; non-retryable 4xx fail fast.
- **Crash recovery:** the watermark `(updated_at, id)` lives only in `pipeline_state`
  and is advanced in the same transaction as the upsert, to the max value actually
  loaded. Re-runs re-read from the watermark minus a configurable overlap (2h); upserts
  make that idempotent.
- **State:** each run downloads the newest valid `state-*.db.gz` (sha256 +
  `PRAGMA integrity_check`). Missing/corrupt state **fails loudly** unless
  `--bootstrap`, which rebuilds via backfill. Uploads use new unique names (no
  delete-then-upload window); before upload the run verifies the newest asset is still
  the one it started from. Schedule and backfill share one `concurrency` group
  (`cancel-in-progress: false`).
- **Backfill:** sliced by month of `created_date`, each slice its own transaction and
  resumable; uses the same upsert path; never moves the incremental watermark.
- **Deletes:** weekly `reconcile-deletes` pulls `unique_key`s for the window and
  soft-deletes rows missing at source (count logged; warn if > 0.5%).
- **DQ noise control:** volume compared with the same weekday over 4 weeks (warn);
  new complaint types (warn); completeness `count(*)` at source vs landed (fail);
  schema drift (fail on missing required columns); bad-close rate per agency (warn).
- **Stale report:** on fail the previous report stays with a staleness banner.
- **Logging:** JSON lines with `run_id`, `step`, counts, durations; secrets redacted.
- **Secrets:** `SODA_APP_TOKEN`, optional `LLM_API_KEY` in Actions secrets / `.env`.
- **Free-tier upkeep:** GitHub disables cron on public repos after 60 days without
  activity; a monthly keepalive check is documented and alerted.

### Agentic AI

The investigator turns a failed check into an engineer-ready issue: evidence bundle
(check, metric vs baseline, top contributing agencies/types, sample rows, last batch
manifests) + rule-based hypotheses (near-zero volume → source outage; missing/new
columns → schema change; one agency > N× baseline → genuine spike). An optional LLM
rewrite uses any OpenAI-compatible endpoint via `LLM_BASE_URL`/`LLM_API_KEY`; it has a
30s timeout and falls back to the template. No criterion depends on it. The scheduled
Claude runner also reads open investigator issues and posts a diagnosis comment,
using the existing subscription ($0 extra).
