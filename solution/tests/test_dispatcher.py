import asyncio
import json
import inspect
from datetime import datetime, timedelta

import httpx

from app.dispatcher import Dispatcher
from app.provider import ProviderClock
from app.repository import Repository
from helpers import completed_result, make_config

NOW = datetime.fromisoformat("2026-11-12T17:30:00+05:30")


def test_priority_batches_fill_capacity_in_callback_retry_fresh_order():
    from types import SimpleNamespace
    from app.domain import CampaignSchedule, CallingWindow

    now = NOW
    schedule = CampaignSchedule(
        campaign=CallingWindow.from_config(
            {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        ),
        regulatory=CallingWindow.from_config({"start": "09:00", "end": "21:00"}),
        grace_seconds=120,
    )

    class BatchRepository:
        campaigns = {"c": SimpleNamespace(tenant_id="tenant_a")}
        schedules = {"c": schedule}

        def __init__(self):
            self.reserved = []

        def active_attempts(self):
            return list(self.reserved)

        def reserve_attempt(self, candidate, _now):
            item = {"request_id": candidate["lead_id"], "tenant_id": "tenant_a"}
            self.reserved.append(item)
            return item

    class BatchConfig:
        tenants = (SimpleNamespace(tenant_id="tenant_a", max_concurrent_calls=2),)

    repo = BatchRepository()
    dispatcher = Dispatcher(
        config=BatchConfig(), repository=repo, provider=object(),
        callback_url="http://127.0.0.1:8200/webhooks/telephony", output_path="unused.csv",
    )
    submitted = []

    async def record_submission(attempt, _now):
        submitted.append(attempt["request_id"])

    dispatcher._submit_reserved_attempt = record_submission
    candidates = [
        {"campaign_id": "c", "lead_id": "fresh_1", "state": "pending"},
        {"campaign_id": "c", "lead_id": "retry_1", "state": "retry_wait"},
        {"campaign_id": "c", "lead_id": "callback_1", "state": "callback_wait"},
        {"campaign_id": "c", "lead_id": "fresh_2", "state": "pending"},
    ]
    dispatched = asyncio.run(dispatcher._dispatch_priority_batches(candidates, now))
    assert dispatched is True
    assert submitted == ["callback_1", "retry_1"]


def test_due_callbacks_block_lower_priority_work_for_same_tenant_when_capacity_is_full():
    from types import SimpleNamespace
    from app.domain import CampaignSchedule, CallingWindow

    schedule = CampaignSchedule(
        campaign=CallingWindow.from_config(
            {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        ),
        regulatory=CallingWindow.from_config({"start": "09:00", "end": "21:00"}),
        grace_seconds=120,
    )

    class BatchRepository:
        campaigns = {"c": SimpleNamespace(tenant_id="tenant_a")}
        schedules = {"c": schedule}

        def __init__(self):
            self.reserved = []

        def active_attempts(self):
            return list(self.reserved)

        def reserve_attempt(self, candidate, _now):
            item = {"request_id": candidate["lead_id"], "tenant_id": "tenant_a"}
            self.reserved.append(item)
            return item

    class BatchConfig:
        tenants = (SimpleNamespace(tenant_id="tenant_a", max_concurrent_calls=1),)

    repo = BatchRepository()
    dispatcher = Dispatcher(
        config=BatchConfig(), repository=repo, provider=object(),
        callback_url="http://127.0.0.1:8200/webhooks/telephony", output_path="unused.csv",
    )
    submitted = []

    async def record_submission(attempt, _now):
        submitted.append(attempt["request_id"])

    dispatcher._submit_reserved_attempt = record_submission
    candidates = [
        {"campaign_id": "c", "lead_id": "callback_1", "state": "callback_wait"},
        {"campaign_id": "c", "lead_id": "callback_2", "state": "callback_wait"},
        {"campaign_id": "c", "lead_id": "retry_1", "state": "retry_wait"},
        {"campaign_id": "c", "lead_id": "fresh_1", "state": "pending"},
    ]

    assert asyncio.run(dispatcher._dispatch_priority_batches(candidates, NOW)) is True
    assert submitted == ["callback_1"]


def test_priority_batch_submits_same_priority_calls_concurrently_to_capacity():
    from types import SimpleNamespace
    from app.domain import CampaignSchedule, CallingWindow

    schedule = CampaignSchedule(
        campaign=CallingWindow.from_config(
            {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        ),
        regulatory=CallingWindow.from_config({"start": "09:00", "end": "21:00"}),
        grace_seconds=120,
    )

    class BatchRepository:
        campaigns = {"c": SimpleNamespace(tenant_id="tenant_a")}
        schedules = {"c": schedule}

        def __init__(self):
            self.reserved = []

        def active_attempts(self):
            return list(self.reserved)

        def reserve_attempt(self, candidate, _now):
            item = {"request_id": candidate["lead_id"], "tenant_id": "tenant_a"}
            self.reserved.append(item)
            return item

    class BatchConfig:
        tenants = (SimpleNamespace(tenant_id="tenant_a", max_concurrent_calls=2),)

    dispatcher = Dispatcher(
        config=BatchConfig(), repository=BatchRepository(), provider=object(),
        callback_url="http://127.0.0.1:8200/webhooks/telephony", output_path="unused.csv",
    )
    in_flight = 0
    max_in_flight = 0
    submitted = []

    async def record_submission(attempt, _now):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        submitted.append(attempt["request_id"])
        try:
            await asyncio.sleep(0.02)
        finally:
            in_flight -= 1

    dispatcher._submit_reserved_attempt = record_submission
    candidates = [
        {"campaign_id": "c", "lead_id": f"fresh_{index}", "state": "pending"}
        for index in range(1, 4)
    ]
    assert asyncio.run(dispatcher._dispatch_priority_batches(candidates, NOW)) is True
    assert len(submitted) == 2
    assert max_in_flight == 2


def test_projected_submission_time_accounts_for_clock_age_and_http_handoff():
    from app.provider import ProviderClock, estimate_submission_time
    from app.domain import CampaignSchedule, CallingWindow, is_allowed_start

    sample = datetime.fromisoformat("2026-11-12T18:58:30+05:30")
    epoch = datetime.fromisoformat("2026-11-12T17:30:00+05:30")
    clock = ProviderClock(now=sample, speed=600.0, sim_epoch=epoch, real_epoch=100.0)
    from unittest.mock import patch
    # Keep the provider's sample consistent with its exposed epochs: at 600x,
    # 88.5 simulated minutes after sim_epoch is 8.85 real seconds after real_epoch.
    real_now = 100.0 + (sample - epoch).total_seconds() / 600.0 + 0.4
    with patch("app.provider.time.time", return_value=real_now):
        projected = estimate_submission_time(
            clock, 100.0, real_now, request_arrival_guard_seconds=0.5
        )
    assert projected == datetime.fromisoformat("2026-11-12T19:07:30+05:30")
    schedule = CampaignSchedule(
        campaign=CallingWindow.from_config(
            {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat"]}
        ),
        regulatory=CallingWindow.from_config({"start": "09:00", "end": "21:00"}),
        grace_seconds=120,
    )
    # The raw provider sample is within its skew allowance, but the projected
    # POST arrival is not. The dispatcher must defer instead of dialling late.
    assert is_allowed_start(sample, schedule)
    assert not is_allowed_start(projected, schedule)


class FakeProvider:
    def __init__(self, responses=None, existing=None, clocks=None):
        self.responses = list(responses or [])
        self.existing = existing
        self.calls = []
        self.clocks = list(clocks or [])

    async def create_call(self, payload):
        self.calls.append(dict(payload))
        code, body = self.responses.pop(0)
        return httpx.Response(code, json=body)

    async def get_call_by_request_id(self, request_id):
        if callable(self.existing):
            value = self.existing(request_id)
            return await value if inspect.isawaitable(value) else value
        return self.existing

    async def get_call_by_sid(self, call_sid):
        if callable(self.existing):
            value = self.existing(call_sid)
            return await value if inspect.isawaitable(value) else value
        return self.existing

    async def get_clock(self):
        now = self.clocks.pop(0) if self.clocks else NOW
        return ProviderClock(now=now, speed=600.0)


def new_attempt(tmp_path, database):
    config = make_config(tmp_path, campaign_count=1)
    repo = Repository(database, config)
    repo.initialize()
    candidate = repo.get_dispatch_candidates(NOW)[0]
    attempt = repo.reserve_attempt(candidate, NOW)
    return config, repo, attempt


def new_dispatcher(config, repo, fake_provider):
    return Dispatcher(
        config=config,
        repository=repo,
        provider=fake_provider,
        callback_url="http://127.0.0.1:8200/webhooks/telephony",
        output_path="final_state.csv",
        max_504_replays=2,
    )


def test_503_is_not_counted_and_releases_reservation(tmp_path, database):
    config, repo, attempt = new_attempt(tmp_path, database)
    provider = FakeProvider(responses=[(503, {"error": "service_unavailable"})])
    dispatcher = new_dispatcher(config, repo, provider)
    import asyncio
    asyncio.run(dispatcher._submit_reserved_attempt(attempt, NOW))
    with repo.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
        lead_state = connection.execute(
            "SELECT state FROM leads WHERE campaign_id = %s AND lead_id = %s",
            (attempt["campaign_id"], attempt["lead_id"]),
        ).fetchone()["state"]
    assert state == "retryable"
    assert lead_state == "pending"
    assert len(repo.active_attempts()) == 0


def test_504_without_provider_record_replays_the_same_request_id(tmp_path, database):
    config, repo, attempt = new_attempt(tmp_path, database)
    accepted = {
        "request_id": attempt["request_id"],
        "call_sid": "CA123",
        "campaign_id": attempt["campaign_id"],
        "lead_id": attempt["lead_id"],
        "state": "active",
        "created_at": NOW.isoformat(),
    }
    provider = FakeProvider(
        responses=[
            (504, {"error": "upstream_timeout"}),
            (201, accepted),
        ],
        existing=None,
    )
    # The first lookup after 504 is a 404; the replay is accepted.
    calls = []
    async def missing_once_then_existing(request_id):
        calls.append(request_id)
        return None if len(calls) == 1 else accepted
    provider.existing = missing_once_then_existing
    dispatcher = new_dispatcher(config, repo, provider)
    import asyncio
    asyncio.run(dispatcher._submit_reserved_attempt(attempt, NOW))
    assert len(provider.calls) == 2
    assert provider.calls[0]["request_id"] == provider.calls[1]["request_id"] == attempt["request_id"]
    with repo.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
    assert state == "accepted"


def test_webhook_result_can_win_race_against_late_api_response(tmp_path, database):
    config, repo, attempt = new_attempt(tmp_path, database)
    result = completed_result(attempt, disposition="interested")
    raw = json.dumps(result, separators=(",", ":")).encode()
    assert repo.apply_webhook(
        delivery_id="delivery-race", raw_body=raw, result=result, received_at=NOW
    )
    # The provider then returns its original 201; it must not move the ended attempt backwards.
    provider_body = {
        "request_id": attempt["request_id"],
        "call_sid": result["call_sid"],
        "campaign_id": attempt["campaign_id"],
        "lead_id": attempt["lead_id"],
        "state": "active",
        "created_at": NOW.isoformat(),
    }
    provider = FakeProvider()
    dispatcher = new_dispatcher(config, repo, provider)
    import asyncio
    asyncio.run(dispatcher._consume_provider_call(provider_body, NOW))
    with repo.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
    assert state == "ended"


def test_504_404_after_clock_skew_window_is_deferred_without_replay(tmp_path, database):
    config, repo, attempt = new_attempt(tmp_path, database)
    # The provider allows +/-120 simulated seconds of clock skew. At 19:02:01,
    # even the -120s probe is beyond the exclusive 19:00 campaign close.
    closing = datetime.fromisoformat("2026-11-12T19:02:01+05:30")
    provider = FakeProvider(
        responses=[(504, {"error": "upstream_timeout"})],
        existing=None,
        clocks=[NOW, closing],
    )
    dispatcher = new_dispatcher(config, repo, provider)
    import asyncio
    asyncio.run(dispatcher._submit_reserved_attempt(attempt, NOW))
    assert len(provider.calls) == 1
    with repo.db.connect() as connection:
        attempt_state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
        lead_state = connection.execute(
            "SELECT state FROM leads WHERE campaign_id = %s AND lead_id = %s",
            (attempt["campaign_id"], attempt["lead_id"]),
        ).fetchone()["state"]
    assert attempt_state == "retryable"
    assert lead_state == "pending"


def test_stale_clock_preflight_releases_reservation_before_post(tmp_path, database):
    import asyncio

    config, repo, attempt = new_attempt(tmp_path, database)
    sample = datetime.fromisoformat("2026-11-12T18:58:30+05:30")

    class DelayedClockProvider(FakeProvider):
        async def get_clock(self):
            await asyncio.sleep(0.4)
            return ProviderClock(now=sample, speed=600.0)

    provider = DelayedClockProvider()
    dispatcher = new_dispatcher(config, repo, provider)
    asyncio.run(dispatcher._submit_reserved_attempt(attempt, NOW))

    assert provider.calls == []
    with repo.db.connect() as connection:
        attempt_state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
        lead_state = connection.execute(
            "SELECT state FROM leads WHERE campaign_id = %s AND lead_id = %s",
            (attempt["campaign_id"], attempt["lead_id"]),
        ).fetchone()["state"]
    assert attempt_state == "retryable"
    assert lead_state == "pending"


def test_restart_reconciliation_finds_accepted_call_by_persisted_request_id(tmp_path, database):
    import asyncio
    import time

    config, repo, attempt = new_attempt(tmp_path, database)
    with repo.db.transaction() as connection:
        connection.execute(
            "UPDATE attempts SET updated_real_at = %s WHERE request_id = %s",
            (time.time() - 10, attempt["request_id"]),
        )
    # Reopen the same DB as a restarted process. No in-memory state is reused.
    restarted_repo = Repository(database, config)
    restarted_repo.initialize()
    provider_call = {
        "request_id": attempt["request_id"],
        "call_sid": "CA-recovered",
        "campaign_id": attempt["campaign_id"],
        "lead_id": attempt["lead_id"],
        "state": "active",
        "created_at": NOW.isoformat(),
    }
    provider = FakeProvider(existing=provider_call)
    dispatcher = new_dispatcher(config, restarted_repo, provider)
    asyncio.run(dispatcher._reconcile(NOW))
    with restarted_repo.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
    assert state == "accepted"


def test_lost_webhook_is_recovered_from_provider_call_result(tmp_path, database):
    import asyncio

    config, repo, attempt = new_attempt(tmp_path, database)
    result = completed_result(attempt, disposition="interested")
    repo.mark_accepted(attempt["request_id"], result["call_sid"], NOW)
    provider_call = {
        "request_id": attempt["request_id"],
        "call_sid": result["call_sid"],
        "campaign_id": attempt["campaign_id"],
        "lead_id": attempt["lead_id"],
        "state": "ended",
        "result": result,
    }
    provider = FakeProvider(existing=provider_call)
    dispatcher = new_dispatcher(config, repo, provider)
    asyncio.run(dispatcher._reconcile(NOW))
    with repo.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
    assert state == "ended"


class CompletingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.now = NOW
        self.placed = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_clock(self):
        self.now = self.now + timedelta(seconds=1)
        return ProviderClock(now=self.now, speed=600.0)

    async def create_call(self, payload):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Yield once so concurrent same-priority submissions can overlap.
            await asyncio.sleep(0.02)
            self.calls.append(dict(payload))
            self.placed.append(payload["request_id"])
            if payload["lead_id"] == "lead_1":
                disposition = "dnd"
            else:
                disposition = "interested"
            result = {
                "event": "call.completed",
                "call_sid": "CA" + payload["request_id"][-24:],
                "request_id": payload["request_id"],
                "tenant_id": "tenant_a",
                "campaign_id": payload["campaign_id"],
                "lead_id": payload["lead_id"],
                "phone": payload["phone"],
                "status": "completed",
                "disposition": disposition,
                "callback_at": None,
                "started_at": self.now.isoformat(),
                "ended_at": (self.now + timedelta(minutes=1)).isoformat(),
                "duration_sec": 60,
            }
            body = {
                "request_id": payload["request_id"],
                "call_sid": result["call_sid"],
                "campaign_id": payload["campaign_id"],
                "lead_id": payload["lead_id"],
                "state": "ended",
                "result": result,
            }
            return httpx.Response(201, json=body)
        finally:
            self.in_flight -= 1


def test_dispatcher_run_reaches_final_states_and_honours_global_dnd(tmp_path, database):
    import asyncio

    config = make_config(tmp_path, tenant_limit=2, campaign_count=2)
    repo = Repository(database, config)
    repo.initialize()
    provider = CompletingProvider()
    dispatcher = Dispatcher(
        config=config,
        repository=repo,
        provider=provider,
        callback_url="http://127.0.0.1:8200/webhooks/telephony",
        output_path=tmp_path / "final_state.csv",
        max_504_replays=1,
    )
    asyncio.run(dispatcher.run())
    assert repo.all_leads_final()
    assert len(provider.placed) == 2
    assert provider.max_in_flight == 2
    import csv
    with (tmp_path / "final_state.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    states = {(row["campaign_id"], row["lead_id"]): row["final_state"] for row in rows}
    assert states[("campaign_1", "lead_1")] == "completed"
    assert states[("campaign_1", "lead_2")] == "completed"
    assert states[("campaign_2", "lead_3")] == "dnd_blocked"


def test_pre_dial_clock_outage_keeps_reservation_for_reconciliation(tmp_path, database):
    import asyncio

    config, repo, attempt = new_attempt(tmp_path, database)
    provider = FakeProvider()

    async def fail_clock():
        raise httpx.ConnectError("temporary provider clock outage")

    provider.get_clock = fail_clock
    dispatcher = new_dispatcher(config, repo, provider)
    asyncio.run(dispatcher._submit_reserved_attempt(attempt, NOW))
    with repo.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
    assert state == "requesting"
    assert not provider.calls


def test_committed_attempt_survives_sigkill_and_process_restart(tmp_path, database):
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    config = make_config(tmp_path, campaign_count=1)
    marker = tmp_path / "reservation_committed.txt"
    child_code = r'''import sys
import time
from pathlib import Path
from datetime import datetime
from app.config import AppConfig
from app.database import Database
from app.repository import Repository
config = AppConfig.load(sys.argv[1])
repo = Repository(Database(sys.argv[2], schema=sys.argv[3]), config)
repo.initialize()
now = datetime.fromisoformat("2026-11-12T17:30:00+05:30")
candidate = repo.get_dispatch_candidates(now)[0]
attempt = repo.reserve_attempt(candidate, now)
assert attempt is not None
Path(sys.argv[4]).write_text(attempt["request_id"], encoding="utf-8")
time.sleep(300)
'''
    solution_dir = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(solution_dir)
    child = subprocess.Popen(
        [
            sys.executable, "-c", child_code, str(config.fixture_path),
            database.dsn, database.schema, str(marker),
        ],
        cwd=solution_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not marker.exists():
            stdout, stderr = child.communicate(timeout=2)
            raise AssertionError(
                f"Child never committed a reservation; rc={child.returncode}; "
                f"stdout={stdout}; stderr={stderr}"
            )
        request_id = marker.read_text(encoding="utf-8")
        child.kill()  # SIGKILL on POSIX; TerminateProcess on Windows.
        child.wait(timeout=5)
        if os.name == "posix":
            assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()

    # A new process/repository recovers the committed reservation from PostgreSQL.
    restarted = Repository(database, config)
    restarted.initialize()
    active = restarted.active_attempts()
    assert len(active) == 1
    assert active[0]["request_id"] == request_id
    assert active[0]["state"] == "requesting"

    # Simulate the original request having died before it reached the provider.
    with restarted.db.transaction() as connection:
        connection.execute(
            "UPDATE attempts SET updated_real_at = %s WHERE request_id = %s",
            (time.time() - 10, request_id),
        )
    accepted = {
        "request_id": request_id,
        "call_sid": "CA-process-recovered",
        "campaign_id": active[0]["campaign_id"],
        "lead_id": active[0]["lead_id"],
        "state": "active",
        "created_at": NOW.isoformat(),
    }
    provider = FakeProvider(responses=[(201, accepted)], existing=None)
    dispatcher = new_dispatcher(config, restarted, provider)
    import asyncio
    asyncio.run(dispatcher._reconcile(NOW))
    assert len(provider.calls) == 1
    assert provider.calls[0]["request_id"] == request_id
    with restarted.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (request_id,)
        ).fetchone()["state"]
    assert state == "accepted"


def test_reconcile_clock_outage_keeps_unknown_attempt_for_later_retry(tmp_path, database):
    import asyncio
    import time

    config, repo, attempt = new_attempt(tmp_path, database)
    with repo.db.transaction() as connection:
        connection.execute(
            "UPDATE attempts SET updated_real_at = %s WHERE request_id = %s",
            (time.time() - 10, attempt["request_id"]),
        )
    provider = FakeProvider(existing=None)

    async def fail_clock():
        raise httpx.ConnectError("temporary provider clock outage")

    provider.get_clock = fail_clock
    dispatcher = new_dispatcher(config, repo, provider)
    asyncio.run(dispatcher._reconcile(NOW))
    with repo.db.connect() as connection:
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
    assert state == "requesting"
    assert not provider.calls


def test_provider_error_body_is_not_persisted_with_possible_customer_pii(tmp_path, database):
    import asyncio

    config, repo, attempt = new_attempt(tmp_path, database)
    provider = FakeProvider(
        responses=[(503, {"error": "service_unavailable", "phone": attempt["phone"]})]
    )
    dispatcher = new_dispatcher(config, repo, provider)
    asyncio.run(dispatcher._submit_reserved_attempt(attempt, NOW))
    with repo.db.connect() as connection:
        last_error = connection.execute(
            "SELECT last_error FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["last_error"]
    assert attempt["phone"] not in last_error
    assert "HTTP 503" in last_error


def test_malformed_success_response_keeps_attempt_unknown_for_reconciliation(tmp_path, database):
    config, repo, attempt = new_attempt(tmp_path, database)
    provider = FakeProvider(responses=[(201, {"invalid": "record"})])
    dispatcher = new_dispatcher(config, repo, provider)
    import asyncio
    asyncio.run(dispatcher._submit_reserved_attempt(attempt, NOW))
    with repo.db.connect() as connection:
        row = connection.execute(
            "SELECT state, last_error FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()
    assert row["state"] == "unknown"
    assert "mismatched call record" in row["last_error"]


def test_malformed_provider_call_record_retains_unknown_reservation(tmp_path, database):
    import asyncio

    config, repo, attempt = new_attempt(tmp_path, database)
    provider = FakeProvider(existing=None)
    dispatcher = new_dispatcher(config, repo, provider)
    asyncio.run(dispatcher._consume_provider_call({
        "request_id": attempt["request_id"],
        "state": "active",
        "created_at": NOW.isoformat(),
    }, NOW))
    with repo.db.connect() as connection:
        row = connection.execute(
            "SELECT state, last_error FROM attempts WHERE request_id = %s",
            (attempt["request_id"],),
        ).fetchone()
    assert row["state"] == "unknown"
    assert "missing a valid call_sid" in row["last_error"]
