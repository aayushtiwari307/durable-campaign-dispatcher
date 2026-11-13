# One-lead crash/restart reproduction (Windows PowerShell)

This is a focused smoke test of the critical crash window. It intentionally uses a one-lead fixture for quick deterministic verification. It must use a fresh database paired with one fresh provider process.

## Start the provider (Terminal A)

From the repository root:

```powershell
Set-Location .\assignment-b-workflow
python -m mock_telephony --fixtures ..\verification-evidence\recovery-fixtures\campaigns.json --speed 1 --seed 7 --failure-scale 0
```

Keep this provider process running for the entire test.

## Start dispatcher in a killable process (Terminal B)

From repository root; create a fresh database first, and replace the example name only if it is unused:

```powershell
docker compose exec -T postgres psql -U au -d postgres -c "CREATE DATABASE au_assignment_crash_test OWNER au;"
```

Start the dispatcher as a process with stdout/stderr redirected:

```powershell
$args = @(
  ".\solution\run.py",
  "--fixtures", ".\verification-evidence\recovery-fixtures\campaigns.json",
  "--database-url", "postgresql://au:au_local_only@127.0.0.1:5432/au_assignment_crash_test",
  "--output", ".\verification-evidence\final_state_crash_test.csv"
)
$p = Start-Process -FilePath "$PWD\.venv\Scripts\python.exe" -ArgumentList $args -WorkingDirectory $PWD -RedirectStandardOutput .\verification-evidence\crash-test.stdout.log -RedirectStandardError .\verification-evidence\crash-test.stderr.log -PassThru
$p.Id | Set-Content .\verification-evidence\crash-test.pid
```

Poll `GET http://127.0.0.1:8099/admin/report` until it shows `calls_total: 1` and `calls_active: 1`, then terminate only the dispatcher:

```powershell
Stop-Process -Id $p.Id -Force
```

Do not stop/restart the provider or change databases/keys.

## Restart the dispatcher

Run the same `Start-Process` command again with the **same** fixture, database URL, and output path (use a new `$p` variable). The restarted service should look up the already-accepted request and poll the provider call by its SID. It must not issue a second accepted call for the same attempt.

When `/status` reports `dispatcher_done: true`, verify:

```powershell
python .\solution\scripts\verify_provider_output.py --csv .\verification-evidence\final_state_crash_test.csv --fixtures .\verification-evidence\recovery-fixtures\campaigns.json
```

Expected invariants: exactly one accepted attempt, one final CSV row, `exact_match: true`, zero provider violations, zero active calls.
