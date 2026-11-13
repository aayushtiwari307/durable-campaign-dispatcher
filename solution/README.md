# Durable Campaign Dispatcher — Assignment B

A PostgreSQL-backed campaign dispatcher for the supplied Agentic Universe mock telephony provider. The supplied `assignment-b-workflow/` fixtures and mock provider are inputs only; do not edit them.

## Stack

- Python 3.11+
- FastAPI + Uvicorn for the dispatcher and authenticated webhook receiver
- PostgreSQL 16 + Psycopg 3 connection pool
- httpx async client for provider calls
- Fernet encryption and keyed HMAC phone tokens for PII at rest
- pytest for scheduling, repository, API, and recovery tests

## Design

- **Durable state machine:** a lead row stores its current workflow state; each provider attempt is a separate row. The deterministic request ID is persisted before making the external request.
- **Ambiguous provider outcomes:** 504s and transport timeouts stay unresolved until the provider is queried by the same request ID. Replays reuse that exact ID.
- **Tenant concurrency:** each reservation locks the tenant row and checks the count of durable active attempts. Reservations from different campaigns share the same tenant lock.
- **Phone exclusivity and DND:** a persistent per-phone guard serializes reservations, result transitions, and DND updates. A PostgreSQL partial unique index is the final cross-process safeguard for active calls on the same phone.
- **Webhook idempotency:** the raw body is verified with HMAC-SHA256; delivery IDs and state transitions are committed in one transaction. The phone guard is acquired before the webhook-event foreign-key insert to preserve lock ordering with reconciliation.
- **Deadlock resilience:** repository transactions that are safe to replay are retried after PostgreSQL deadlock or serialization aborts. Provider side effects are never inside these retryable transactions.
- **Recovery:** unresolved attempts are looked up by request ID; accepted attempts are polled by call SID so a lost webhook does not strand a lead.
- **Scheduling:** the provider's simulated clock is authoritative. The dispatcher uses `sim_epoch`/`real_epoch` to project the provider clock to the following POST handoff, and submits due callbacks, retries, then fresh leads in same-priority batches up to tenant capacity.
- **PII:** phone numbers and customer names are Fernet-encrypted. A keyed HMAC token supports phone uniqueness and DND checks without storing plaintext phone numbers in database indexes. Provider response bodies are not logged or copied wholesale into result snapshots.
- **Single scheduler owner:** a PostgreSQL advisory lock prevents multiple dispatcher loops from running against one state database while allowing the API workers to receive webhooks.

## Setup

Run from the repository root. The original `docker-compose.yml` uses PostgreSQL 16 on host port `5432`, database `au_assignment`, user `au`, and the local-only password `au_local_only`. Do not reuse these local credentials outside a local development environment.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r solution\requirements.txt
docker compose up -d postgres
```

Create the root `.env` once. Keep the generated Fernet key stable across restarts that use the same database:

```powershell
$piiKey = python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
@"
DATABASE_URL=postgresql://au:au_local_only@127.0.0.1:5432/au_assignment
PII_ENCRYPTION_KEY=$piiKey
"@ | Set-Content -Encoding ascii .env
```

### macOS / Linux (Bash)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r solution/requirements.txt
docker compose up -d postgres
```

Create the root `.env` once:

```bash
PII_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
printf 'DATABASE_URL=%s\nPII_ENCRYPTION_KEY=%s\n' \
  'postgresql://au:au_local_only@127.0.0.1:5432/au_assignment' "$PII_KEY" > .env
chmod 600 .env
```

The runner loads `.env` from the repository root. Never commit `.env` or share its key. Changing or losing `PII_ENCRYPTION_KEY` makes existing encrypted PII unreadable. `.env.example` documents the required variables.

## Run the provider and dispatcher

Use two terminals. Start the unchanged mock provider first and leave it running.

**Terminal A — mock provider (PowerShell):**

```powershell
.\.venv\Scripts\Activate.ps1
Set-Location .\assignment-b-workflow
python -m mock_telephony --failure-scale 0
```

**Terminal A — mock provider (macOS / Linux):**

```bash
source .venv/bin/activate
cd assignment-b-workflow
python -m mock_telephony --failure-scale 0
```

The mock provider listens on `http://127.0.0.1:8099`. Omit `--failure-scale 0` (or use `--failure-scale 1`) to enable the provider's default failure injection. Use `--seed 42` for a deterministic alternate seed.

**Terminal B — dispatcher (PowerShell):** return to the repository root:

```powershell
Set-Location ..
python solution\run.py --output final_state.csv
```

**Terminal B — dispatcher (macOS / Linux):** from the repository root:

```bash
python solution/run.py --output final_state.csv
```

The API listens on `127.0.0.1:8200`. `GET /health` checks API/database health and `GET /status` shows campaign counts, active attempts, scheduler status, and any scheduler error. The API process intentionally remains alive after dispatch finishes so it can serve webhooks and status requests. Completion is indicated by `dispatcher_done: true` and `dispatcher_error: null` from `/status`; it does not necessarily return to a shell prompt. The CSV is written after all leads are final and no active attempts remain.

## Tests

The integration tests create isolated temporary PostgreSQL schemas; they do not truncate the database's `public` schema. Point `TEST_DATABASE_URL` at a disposable PostgreSQL database (it may be the same PostgreSQL server/database used for local development because each test uses a separate schema):

```powershell
$env:TEST_DATABASE_URL = $env:DATABASE_URL
python -m pytest solution\tests -q --strict-config --strict-markers
python -m compileall -q solution
```

A test run without `TEST_DATABASE_URL` skips the PostgreSQL integration tests; it is not a full verification.

## End-to-end verification

1. Start the provider with `--failure-scale 0` and a fresh empty dispatcher database.
2. Start the dispatcher and wait for `/status` to report `dispatcher_done: true` with `dispatcher_error: null`.
3. Run the provider verifier from the repository root:

   ```powershell
   python solution\scripts\verify_provider_output.py --csv final_state.csv --fixtures assignment-b-workflow/fixtures/campaigns.json
   ```

   It compares every CSV row with `/admin/leads`, checks exact columns/order and attempts, requires zero provider violations and no active calls, and checks observed tenant concurrency against fixture limits. Review `/admin/report`'s `callback_lateness_minutes` as a performance metric too.
4. Repeat with the provider's default failure scale, using a new provider process and a fresh dispatcher database for that independent run.
5. For crash recovery, keep the same provider process and database running, terminate only the dispatcher process, restart it with the same database and `.env`, then run the verifier after completion. Do not delete the PostgreSQL volume during a recovery test.

A new provider process resets the provider's in-memory world and simulated clock. Do not point it at a database containing attempts from a previous provider run; use a fresh database/schema for a clean independent run. Conversely, a crash/restart test must reuse the same provider process and database so the external and durable states remain paired.

## Verification boundary

The automated tests are necessary but are not proof of a clean provider run. Before submission, execute the PostgreSQL-enabled test suite and the end-to-end checks above, including a default-failure-scale run, an actual process-kill/restart, zero provider violations, zero active calls, and an exact CSV comparison with `/admin/leads`. Do not report those checks as passed unless their actual outputs confirm them.
