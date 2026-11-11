# Status

**Next action:** trigger `live-smoke` on GitHub Actions, review `sample_rows.json`
(real timestamp formats, `:updated_at` literal acceptance), then start M2 (transforms
with `close_quality` + weekly cohort marts).

| Milestone | State | Proves criteria | Tests |
|---|---|---|---|
| M0 design, decisions, skeleton, CI | done | 7 (partial) | `ci.yml` |
| M1 extract + idempotent load + state | done locally; live smoke pending | 1, 2, 3, 5 | `tests/unit/test_soda_retry.py`, `tests/integration/test_extract_load_flow.py` |
| M2 transforms, close quality, DQ, quarantine | todo | 4 (partial) | — |
| M3 report, Pages, scheduled pipeline, release-asset state, backfill workflow | todo | 6 | — |
| M4 investigator | todo | 4 | — |
| M5 delete reconciliation, README, fresh-checkout verification | todo | 7 | — |

## Known risks / open questions
- Real SODA behaviour of `:updated_at` literals in `$where` is unverified (dev box has
  no network to NYC Open Data); `live-smoke` checks it.
- `SODA_APP_TOKEN` repository secret not yet set (optional; needed before daily schedule).
