# Local verification evidence (2026-11-13)

The values below are transcribed from the actual local test/verifier output. Generated final-state CSV outputs are deliberately excluded from the repository in accordance with the assignment instruction not to commit generated data. The verifier outputs confirmed exact matches to the mock provider's `/admin/leads` for each recorded run.

## Automated suite

- Command: `python -m pytest solution/tests -q --strict-config --strict-markers`
- Result reported from local PostgreSQL-backed environment: **75 passed** (one non-fatal warning was reported in the earlier run).
- `python -m compileall -q solution`: exit code `0`.

## Full clean run (`--failure-scale 0`)

- Fixture set: supplied 200-lead / 3-campaign fixtures.
- Accepted calls: **407**.
- CSV rows: **200**.
- Exact provider match: `true`; mismatches: `0`.
- Provider violation count: **0**.
- Provider active calls at verification: **0**.
- Provider leads: **200**.
- Observed max concurrency: `acme_insurance: 4`, `zen_retail: 3`; both within configured limits.
- Callback lateness: count `64`, p50 `8.8` min, p90 `32.2` min, max `72.7` min.

## Full failure-injection run (`--seed 42 --failure-scale 1`)

- Fixture set: supplied 200-lead / 3-campaign fixtures.
- Accepted calls: **408**.
- CSV rows: **200**.
- Exact provider match: `true`; mismatches: `0`.
- Provider violation count: **0**.
- Provider active calls at verification: **0**.
- Provider leads: **200**.
- Observed max concurrency: `acme_insurance: 4`, `zen_retail: 3`; both within configured limits.
- Callback lateness: count `71`, p50 `10.5` min, p90 `37.1` min, max `3836.9` min.
- The maximum lateness is a visible tail outlier. The fixture includes campaigns with restricted calling days (including weekday-only operation), so long calendar closures can contribute to measured lateness. This should be reviewed separately as a scheduling-performance metric; it did not coincide with a provider rule violation in this run.

## Live process-kill / restart recovery smoke test

- Provider and PostgreSQL were kept running.
- Only the dispatcher process was forcefully terminated while the provider reported one active call.
- Dispatcher restarted with the **same provider process**, same database, same fixtures, same output target, and same PII key.
- Recovery found the original provider call by call SID, fetched its ended result, and applied it to PostgreSQL.
- Provider call SID: `CA4b0366cc39d1434a8afd9d66`.
- Original request ID: `au_579c47215b62072552fa79ba7fe40350`.
- Provider result: `completed` / `interested`.
- PostgreSQL: one accepted attempt (`attempt_no=1`), attempt state `ended`, lead state `completed`.
- Provider report: calls total `1`, active `0`, webhook deliveries `1`, violations `0`.
- CSV verifier: one row, one accepted attempt, exact match `true`, mismatch count `0`, zero provider violations, zero active calls.

This process-kill test used the included one-lead recovery fixture to isolate the crash window. It is a live recovery smoke test, not a claim that the full 200-lead workload was force-killed multiple times. The state-machine and repository tests additionally cover reservation, uncertain outcomes, reconciliation, idempotency, and scheduling behavior.

## Evidence boundary

These results prove the specific local runs recorded above. They do not guarantee every unseen seed, arbitrary process-kill timing, or environment will behave identically. The grading harness should independently run its unseen-seed and kill/restart scenarios against the checked-in source.
