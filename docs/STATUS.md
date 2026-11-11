# Status

**Next action:** start M2: full-rebuild transforms (`fct_requests` with `close_quality`
flags and UTC durations, `mart_closure_weekly` cohorts, `mart_degradation`), then the
DQ suite. Also: write run summaries to `$GITHUB_STEP_SUMMARY` (artifact downloads are
blocked from the runner workspace) and bump actions to Node 24 versions.

| Milestone | State | Proves criteria | Tests |
|---|---|---|---|
| M0 design, decisions, skeleton, CI | done | 7 (partial) | `ci.yml` |
| M1 extract + idempotent load + state | done; live-verified 2026-11-11 | 1, 2, 3, 5 | `tests/unit/test_soda_retry.py`, `tests/integration/test_extract_load_flow.py` |
| M2 transforms, close quality, DQ, quarantine | todo | 4 (partial) | — |
| M3 report, Pages, scheduled pipeline, release-asset state, backfill workflow | todo | 6 | — |
| M4 investigator | todo | 4 | — |
| M5 delete reconciliation, README, fresh-checkout verification | todo | 7 | — |

## Live verification log
- 2026-11-11 run 43066933475: FAILED, HTTP 400 "Star selection" → fixed in 93a61ba
  (explicit column projection + regression test).
- 2026-11-11 run 43067207185: 2-day window, 10,809 rows loaded, 0 quarantined; rerun
  inserted 0 / unchanged 10,809, identical content checksum. `:updated_at` literals with
  `Z` accepted. Source refreshes in bulk (all rows share a narrow `:updated_at` band), so
  the 2h overlap re-reads the latest daily batch: correct, costs ~10s.

## Known risks / open questions
- `SODA_APP_TOKEN` repository secret not yet set (optional; needed before daily schedule).
