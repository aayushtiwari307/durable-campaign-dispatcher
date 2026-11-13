# Durable Campaign Dispatcher

A PostgreSQL-backed campaign dispatcher that schedules outbound calls through a mock telephony provider while enforcing calling windows, tenant concurrency, phone-level exclusivity, retries, callbacks, global DND, webhook integrity, and crash recovery.

This repository contains the implementation for the **Durable Campaign Dispatcher** workflow. The original assignment brief is preserved at [docs/ORIGINAL_ASSIGNMENT_README.md](docs/ORIGINAL_ASSIGNMENT_README.md).

## Highlights

- **Durable state machine:** PostgreSQL persists lead state, call attempts, deterministic provider request IDs, and recovery information.
- **Safe provider retries:** ambiguous timeouts and 504 responses are reconciled using the original request ID; accepted calls are not blindly re-submitted with a new identity.
- **Scheduling constraints:** campaign and regulatory calling windows, callback/retry/fresh-lead priority, shared tenant concurrency, and one active call per phone.
- **Global opt-out:** DND is enforced across campaigns using a keyed phone token.
- **Webhook security:** raw-body HMAC-SHA256 verification, idempotent event processing, and recovery polling for lost webhooks.
- **PII protection:** names and phone numbers are encrypted at rest; deterministic HMAC tokens support matching without indexing plaintext phone numbers.
- **Operational visibility:** FastAPI health and status endpoints expose database health, progress, active calls, and scheduler errors.

## Technology

Python 3.11+, FastAPI, Uvicorn, PostgreSQL 16, Psycopg 3, httpx, pytest, Fernet encryption, and HMAC-SHA256.

## Quick start

The detailed Windows PowerShell and macOS/Linux instructions are in [solution/README.md](solution/README.md). The following is the short local setup from the repository root.

### 1. Create an environment and install dependencies

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r solution\requirements.txt
docker compose up -d postgres
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r solution/requirements.txt
docker compose up -d postgres
```

Create a local `.env` with `DATABASE_URL` and a generated `PII_ENCRYPTION_KEY` as described in [solution/README.md](solution/README.md). Keep the key stable for a database across restarts. Never commit the populated `.env` file.

### 2. Start the mock provider

In Terminal A, from the repository root:

```bash
cd assignment-b-workflow
python -m mock_telephony --failure-scale 0
```

The provider listens at `http://127.0.0.1:8099`. Use its default failure scale (or `--failure-scale 1`) to exercise injected provider failures.

### 3. Start the dispatcher

In Terminal B, from the repository root:

```bash
python solution/run.py --output final_state.csv
```

The dispatcher API listens at `http://127.0.0.1:8200`.

- `GET /health` — API and database health.
- `GET /status` — campaign progress, active attempts, scheduler status, and scheduler errors.

The API process remains alive after dispatch completes so it can continue receiving webhooks. Completion is reported by `dispatcher_done: true` and `dispatcher_error: null` from `/status`.

## Tests and verification

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database, then run from the repository root:

```bash
python -m pytest solution/tests -q --strict-config --strict-markers
python -m compileall -q solution
```

To compare a completed run against the provider's authoritative lead state:

```bash
python solution/scripts/verify_provider_output.py --csv final_state.csv --fixtures assignment-b-workflow/fixtures/campaigns.json
```

The verifier checks exact CSV rows and attempts, provider violations, active calls, and observed tenant concurrency. See [verification-evidence/VERIFICATION_RESULTS.md](verification-evidence/VERIFICATION_RESULTS.md) for the actual locally observed automated, clean-run, failure-injection, and one-lead crash/restart smoke-test results.

### Observed local validation

| Scenario | Leads | Accepted attempts | CSV match | Provider violations |
|---|---:|---:|---|---:|
| Clean run | 200 | 407 | Exact | 0 |
| Failure injection (seed 42) | 200 | 408 | Exact | 0 |
| Dispatcher kill/restart smoke test | 1 | 1 | Exact | 0 |

The failure-injection run had a callback-lateness maximum of **3,836.9 minutes** (p50 10.5 minutes; p90 37.1 minutes). This is documented as a performance tail outlier in [SUBMISSION.md](SUBMISSION.md) and should not be interpreted as a provider-rule violation. The crash/restart result is a focused one-lead smoke test, not a repeated full-workload crash campaign.

## Repository guide

- `solution/app/` — dispatcher API, scheduling, repository, provider client, webhook handling, and PII utilities.
- `solution/migrations/` — PostgreSQL schema.
- `solution/tests/` — automated tests.
- `solution/scripts/` — provider-output verification.
- `assignment-b-workflow/` — supplied fixtures and unchanged mock provider.
- `verification-evidence/` — concise verification summaries and the one-lead recovery fixture.
- `SUBMISSION.md` — state model, correctness decisions, measured validation, and known limitations.
- `AI_USAGE.md` — AI assistance disclosure.

## Security and data handling

- Do not commit `.env`, generated database files, or real customer data.
- The included data and mock provider are synthetic assignment fixtures.
- Rotate local-only credentials before using any shared environment.
