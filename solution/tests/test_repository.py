import csv

import pytest
from datetime import datetime, timedelta

from app.domain import LeadState
from app.repository import Repository
from helpers import completed_result, make_config

NOW = datetime.fromisoformat("2026-11-12T17:30:00+05:30")


def new_repo(tmp_path, database, *, tenant_limit=2, campaign_count=2):
    config = make_config(tmp_path, tenant_limit=tenant_limit, campaign_count=campaign_count)
    repo = Repository(database, config)
    repo.initialize()
    return repo


def find_candidate(repo, campaign_id, lead_id):
    return next(
        item for item in repo.get_dispatch_candidates(NOW)
        if item["campaign_id"] == campaign_id and item["lead_id"] == lead_id
    )


def test_reservation_persists_request_id_and_prevents_same_phone_cross_campaign(tmp_path, database):
    repo = new_repo(tmp_path, database)
    first = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    assert first is not None
    assert first["state"] == "requesting"
    assert first["request_id"].startswith("au_")
    # Same phone is present in campaign_2 and cannot be reserved while active.
    second = repo.reserve_attempt(find_candidate(repo, "campaign_2", "lead_3"), NOW)
    assert second is None
    assert repo.active_attempts()[0]["request_id"] == first["request_id"]


def test_tenant_capacity_is_shared_across_campaigns(tmp_path, database):
    repo = new_repo(tmp_path, database, tenant_limit=1)
    first = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    second = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_2"), NOW)
    assert first is not None
    assert second is None


def test_503_retry_reuses_same_key_and_does_not_count_as_attempt(tmp_path, database):
    repo = new_repo(tmp_path, database, tenant_limit=1, campaign_count=1)
    candidate = find_candidate(repo, "campaign_1", "lead_1")
    first = repo.reserve_attempt(candidate, NOW)
    repo.mark_retryable(first["request_id"], NOW, "503 service unavailable", delay_seconds=0)
    second = repo.reserve_attempt(candidate, NOW + timedelta(seconds=1))
    assert second is not None
    assert second["request_id"] == first["request_id"]
    assert second["attempt_no"] == 1


def test_accepted_attempt_is_counted_once_when_result_is_applied_twice(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    result = completed_result(attempt, disposition="interested")
    repo.mark_accepted(attempt["request_id"], result["call_sid"], NOW)
    assert repo.apply_provider_result(result) is True
    assert repo.apply_provider_result(result) is False
    with repo.db.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE campaign_id = %s AND lead_id = %s AND state IN ('accepted','ended')",
            ("campaign_1", "lead_1"),
        ).fetchone()["count"]
        state = connection.execute(
            "SELECT state FROM leads WHERE campaign_id = %s AND lead_id = %s",
            ("campaign_1", "lead_1"),
        ).fetchone()["state"]
    assert count == 1
    assert state == LeadState.COMPLETED



def test_ended_request_rejects_a_conflicting_provider_call_sid(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    repo.mark_accepted(attempt["request_id"], "CA-original", NOW)
    result = completed_result(attempt, disposition="interested")
    result["call_sid"] = "CA-original"
    assert repo.apply_provider_result(result) is True

    conflicting_replay = dict(result, call_sid="CA-different")
    with pytest.raises(ValueError, match="conflicting call_sid"):
        repo.apply_provider_result(conflicting_replay)


def test_provider_result_cannot_turn_a_rejected_503_into_an_accepted_attempt(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    repo.mark_retryable(attempt["request_id"], NOW, "provider returned HTTP 503", delay_seconds=0)
    result = completed_result(attempt, disposition="interested")
    with pytest.raises(ValueError, match="non-active request_id"):
        repo.apply_provider_result(result)

def test_dnd_blocks_same_phone_in_other_campaign_but_completes_source_lead(tmp_path, database):
    repo = new_repo(tmp_path, database)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    result = completed_result(attempt, disposition="dnd")
    repo.mark_accepted(attempt["request_id"], result["call_sid"], NOW)
    repo.apply_provider_result(result)
    with repo.db.connect() as connection:
        states = {
            (row["campaign_id"], row["lead_id"]): row["state"]
            for row in connection.execute(
                "SELECT campaign_id, lead_id, state FROM leads WHERE phone_token = %s",
                (repo.pii.phone_token("+919000000001"),)
            ).fetchall()
        }
        guard = connection.execute(
            "SELECT phone_token FROM phone_guards WHERE phone_token = %s",
            (repo.pii.phone_token("+919000000001"),)
        ).fetchone()
    assert states[("campaign_1", "lead_1")] == LeadState.COMPLETED
    assert states[("campaign_2", "lead_3")] == LeadState.DND_BLOCKED
    assert guard["phone_token"] == repo.pii.phone_token("+919000000001")


def test_retry_due_time_is_derived_from_ended_at(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    result = completed_result(attempt, disposition=None, status="no_answer", ended_at="2026-11-12T17:35:00+05:30")
    repo.mark_accepted(attempt["request_id"], result["call_sid"], NOW)
    repo.apply_provider_result(result)
    with repo.db.connect() as connection:
        row = connection.execute(
            "SELECT state, next_eligible_at FROM leads WHERE campaign_id = %s AND lead_id = %s",
            ("campaign_1", "lead_1"),
        ).fetchone()
    assert row["state"] == LeadState.RETRY_WAIT
    assert row["next_eligible_at"].astimezone(NOW.tzinfo).isoformat() == "2026-11-12T18:35:00+05:30"


def test_webhook_receipt_and_dedupe_are_atomic(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    result = completed_result(attempt, disposition="interested")
    import json
    body = json.dumps(result, separators=(",", ":")).encode()
    received_at = NOW + timedelta(seconds=1)
    assert repo.apply_webhook(
        delivery_id="delivery-1", raw_body=body, result=result, received_at=received_at
    ) is True
    assert repo.apply_webhook(
        delivery_id="delivery-1", raw_body=body, result=result, received_at=received_at
    ) is False
    with repo.db.connect() as connection:
        receipts = connection.execute("SELECT COUNT(*) AS count FROM webhook_events").fetchone()["count"]
        state = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()["state"]
    assert receipts == 1
    assert state == "ended"


def test_final_csv_uses_provider_contract_and_sorted_order(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    # Complete lead_1.
    first = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    first_result = completed_result(first, disposition="not_interested")
    repo.mark_accepted(first["request_id"], first_result["call_sid"], NOW)
    repo.apply_provider_result(first_result)

    # Exhaust lead_2 with three accepted no-answer attempts and real retry delays.
    current = datetime.fromisoformat("2026-11-12T10:00:00+05:30")
    ended_times = [
        "2026-11-12T10:05:00+05:30",
        "2026-11-12T11:10:00+05:30",
        "2026-11-12T12:15:00+05:30",
    ]
    for attempt_no, ended_at in enumerate(ended_times, start=1):
        candidates = repo.get_dispatch_candidates(current)
        candidate = next(item for item in candidates if item["lead_id"] == "lead_2")
        attempt = repo.reserve_attempt(candidate, current)
        assert attempt is not None and attempt["attempt_no"] == attempt_no
        result = completed_result(attempt, disposition=None, status="no_answer", ended_at=ended_at)
        repo.mark_accepted(attempt["request_id"], result["call_sid"], current)
        repo.apply_provider_result(result)
        current = datetime.fromisoformat(ended_at) + timedelta(minutes=61)

    output = repo.export_final_state(tmp_path / "final_state.csv")
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["lead_id"] for row in rows] == ["lead_1", "lead_2"]
    assert rows[0] == {
        "campaign_id": "campaign_1", "lead_id": "lead_1", "final_state": "completed",
        "attempts": "1", "last_status": "completed", "last_disposition": "not_interested",
    }
    assert rows[1]["final_state"] == "exhausted"
    assert rows[1]["attempts"] == "3"
    assert rows[1]["last_status"] == "no_answer"


def test_concurrent_reservations_cannot_exceed_shared_tenant_limit(tmp_path, database):
    from concurrent.futures import ThreadPoolExecutor

    repo_a = new_repo(tmp_path, database, tenant_limit=1)
    # A second repository instance represents another worker/process sharing the same DB.
    repo_b = Repository(database, repo_a.config)
    repo_b.initialize()
    candidate_a = find_candidate(repo_a, "campaign_1", "lead_1")
    candidate_b = find_candidate(repo_b, "campaign_1", "lead_2")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(repo_a.reserve_attempt, candidate_a, NOW),
            pool.submit(repo_b.reserve_attempt, candidate_b, NOW),
        ]
        results = [future.result() for future in futures]
    assert sum(result is not None for result in results) == 1
    assert len(repo_a.active_attempts()) == 1


def test_database_rejects_a_different_fixture_seed(tmp_path, database):
    first = new_repo(tmp_path, database, campaign_count=1)
    # Change an input fixture after initial seeding; restarting against the same
    # persistent DB must fail rather than silently mixing two grading seeds.
    leads_path = first.config.fixture_path.parent / "leads_c1.csv"
    leads_path.write_text(
        "lead_id,phone,customer_name\nlead_1,+919000000001,Changed\nlead_2,+919000000002,Two\n",
        encoding="utf-8",
    )
    changed = Repository(database, make_config(tmp_path, campaign_count=1))
    # make_config rewrites the original CSV, so mutate it again after loading.
    (changed.config.fixture_path.parent / "leads_c1.csv").write_text(
        "lead_id,phone,customer_name\nlead_1,+919000000099,Changed\nlead_2,+919000000002,Two\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="different fixture seed"):
        changed.initialize()


def test_dnd_result_serializes_with_concurrent_phone_reservation(tmp_path, database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    repo = new_repo(tmp_path, database, tenant_limit=2, campaign_count=2)
    source = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    assert source is not None
    dnd_result = completed_result(source, disposition="dnd")
    repo.mark_accepted(source["request_id"], dnd_result["call_sid"], NOW)
    other_candidate = find_candidate(repo, "campaign_2", "lead_3")
    barrier = Barrier(2)

    def apply_dnd():
        barrier.wait(timeout=5)
        return repo.apply_provider_result(dnd_result)

    def reserve_same_phone():
        barrier.wait(timeout=5)
        return repo.reserve_attempt(other_candidate, NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        dnd_future = pool.submit(apply_dnd)
        reserve_future = pool.submit(reserve_same_phone)
        assert dnd_future.result(timeout=10) is True
        assert reserve_future.result(timeout=10) is None

    with repo.db.connect() as connection:
        blocked = connection.execute(
            "SELECT state FROM leads WHERE campaign_id = %s AND lead_id = %s",
            ("campaign_2", "lead_3"),
        ).fetchone()["state"]
        active = connection.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE phone_token = %s AND state IN ('prepared','requesting','unknown','accepted')",
            (repo.pii.phone_token("+919000000001"),),
        ).fetchone()["count"]
    assert blocked == LeadState.DND_BLOCKED
    assert active == 0


def test_stale_503_cannot_release_a_newer_accepted_call(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    repo.mark_accepted(attempt["request_id"], "CA-accepted", NOW)

    # A delayed handler for an earlier transport response must not downgrade accepted.
    repo.mark_retryable(attempt["request_id"], NOW, "stale 503", delay_seconds=0)
    with repo.db.connect() as connection:
        row = connection.execute(
            "SELECT state, call_sid FROM attempts WHERE request_id = %s",
            (attempt["request_id"],),
        ).fetchone()
        lead = connection.execute(
            "SELECT state FROM leads WHERE campaign_id = %s AND lead_id = %s",
            (attempt["campaign_id"], attempt["lead_id"]),
        ).fetchone()
    assert row["state"] == "accepted"
    assert row["call_sid"] == "CA-accepted"
    assert lead["state"] == LeadState.DIALING
    assert len(repo.active_attempts()) == 1


def test_pii_is_encrypted_at_rest_and_phone_token_is_keyed(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    with repo.db.connect() as connection:
        lead = connection.execute(
            "SELECT phone_ciphertext, phone_token, customer_name_ciphertext FROM leads "
            "WHERE campaign_id = %s AND lead_id = %s",
            ("campaign_1", "lead_1"),
        ).fetchone()
    assert b"+919000000001" not in bytes(lead["phone_ciphertext"])
    assert b"One" not in bytes(lead["customer_name_ciphertext"])
    assert lead["phone_token"] == repo.pii.phone_token("+919000000001")
    assert lead["phone_token"] != "+919000000001"
    assert repo.pii.decrypt(lead["phone_ciphertext"]) == "+919000000001"
    assert bytes(lead["customer_name_ciphertext"]) != b"One"
    assert repo.pii.decrypt(lead["customer_name_ciphertext"]) == "One"


def test_stale_timeout_cannot_downgrade_retryable_attempt(tmp_path, database):
    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    repo.mark_retryable(attempt["request_id"], NOW, "503", delay_seconds=0)
    repo.mark_unknown(attempt["request_id"], NOW, "late timeout callback")
    with repo.db.connect() as connection:
        row = connection.execute(
            "SELECT state FROM attempts WHERE request_id = %s", (attempt["request_id"],)
        ).fetchone()
    assert row["state"] == "retryable"


def test_attempt_and_result_snapshot_do_not_persist_plaintext_phone(tmp_path, database):
    import json

    repo = new_repo(tmp_path, database, campaign_count=1)
    attempt = repo.reserve_attempt(find_candidate(repo, "campaign_1", "lead_1"), NOW)
    result = completed_result(attempt, disposition="interested")
    repo.mark_accepted(attempt["request_id"], result["call_sid"], NOW)
    repo.apply_provider_result(result)
    with repo.db.connect() as connection:
        row = connection.execute(
            "SELECT phone_ciphertext, phone_token, result_json FROM attempts WHERE request_id = %s",
            (attempt["request_id"],),
        ).fetchone()
    assert bytes(row["phone_ciphertext"]) != b"+919000000001"
    assert row["phone_token"] == repo.pii.phone_token("+919000000001")
    snapshot = row["result_json"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    assert "phone" not in snapshot
    assert "+919000000001" not in json.dumps(snapshot)


def test_transaction_conflicts_are_retried_after_rollback(monkeypatch):
    from psycopg.errors import DeadlockDetected
    from app.repository import retry_transaction_conflicts

    class Probe:
        def __init__(self):
            self.calls = 0

        @retry_transaction_conflicts
        def run(self):
            self.calls += 1
            if self.calls < 3:
                raise DeadlockDetected("simulated deadlock")
            return "committed"

    monkeypatch.setattr("app.repository.time.sleep", lambda _delay: None)
    probe = Probe()
    assert probe.run() == "committed"
    assert probe.calls == 3
