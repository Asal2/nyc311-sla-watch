# Decision record

Reviews of design v1 (2026-11-11): **R** = reliability/correctness reviewer,
**C** = complexity/cost reviewer.

| # | Decision | Source | Reasoning |
|---|---|---|---|
| D1 | Keyset pagination on `(:updated_at, :id)`, never `$offset` | R1 | Offsets over a mutating filter skip/duplicate rows. |
| D2 | Watermark only in `pipeline_state`, same transaction as upsert, set to max loaded value | R2 | One source of truth; crash can't advance it past unloaded data; runner disk is ephemeral. |
| D3 | Upsert: update only if incoming `updated_at` is newer, or equal with a different row hash | R8, C12 | Replays and out-of-order batches become no-ops. |
| D4 | Quarantining a newer version marks the stored key `stale_quarantined` | R8 | Otherwise an outdated version silently feeds metrics. |
| D5 | Headline metric = % closed within N days per created-week cohort, only for mature cohorts | R4 | Median time-to-close on recent requests is biased toward fast closes. |
| D6 | `close_quality` flags, excluded from metrics; alert on per-agency rates | R5 | Sentinel 1900 dates, future/negative closes and instant duplicate closes are real in 311. |
| D7 | zoneinfo NY conversion with `fold`; UTC durations; NY-local window boundaries | R6 | Floating local timestamps + DST give negative/ambiguous durations otherwise. |
| D8 | Weekly delete reconciliation (soft delete) | R7 | `:updated_at` filters never return removed rows. |
| D9 | Full rebuild of facts/marts each run (cut partition-aware rebuilds) | R10, C4 | Seconds at ~1M rows; removes a class of bugs; makes checksums trivial. |
| D10 | State = uniquely named release assets + sha256 + integrity check; keep 7; fail loudly without `--bootstrap`; shared concurrency group | R3, C2 | C proposed a single clobbered asset; R showed clobber = delete-then-upload window and silent empty-DB risk. Took R's version; same cost. |
| D11 | Rejected: commit DB / data branch / Actions cache as durable state | C2 | History bloat or 7-day eviction. Cache is not used. |
| D12 | Landing batches kept as 14-day Actions artifacts; manifests mirrored into a `batches` table | C5, R12 | No pruning code; evidence bundle reads manifests from the DB. |
| D13 | SQLite (stdlib); no DuckDB, no storage abstraction; no pandas | C6, C7 | Dev box can't install packages; abstraction was YAGNI. |
| D14 | CI pinned to Python 3.13 (matches dev box) | C7 | Reproducibility between local pre-push tests and CI. |
| D15 | LLM: GitHub Models retired 2026-07-30, so investigator is deterministic + optional OpenAI-compatible endpoint with fallback; Claude runner comments on issues | C1 | $0 constraint; nothing in the completion criteria may depend on a third-party free tier. |
| D16 | DQ: same-weekday volume baseline (warn), completeness and schema drift (fail), min cohort size 30, source lag vs pipeline lag | R11 | Reduce noise from storms/holidays/bulk republishes; catch silent truncation. |
| D17 | Free SODA app token in CI | C3 | Shared Actions IPs get throttled without it. |
| D18 | Cron at 06:17 UTC; keepalive for the 60-day inactivity rule | C9, R12 | Top-of-hour cron is delayed most; scheduled workflows auto-disable. |
| D19 | `docs/STATUS.md` with milestone → criteria → tests and a "next action" line | C11 | Short autonomous sessions must resume without context. |
| D20 | Kept ruff in `make lint` locally | C10 | Reviewer assumed ruff was missing; it is installed on the dev box. |
