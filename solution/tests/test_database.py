from datetime import datetime

import pytest
from psycopg.errors import UniqueViolation

from app.database import Database
from app.pii import PiiProtector

NOW = datetime.fromisoformat("2026-11-12T17:30:00+05:30")


def seed_minimum(db: Database) -> None:
    db.initialize()
    pii = PiiProtector.from_env()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO tenants(tenant_id, max_concurrent_calls) VALUES (%s, %s)",
            ("tenant_a", 2),
        )
        connection.execute(
            "INSERT INTO campaigns(campaign_id, tenant_id, window_start, window_end, window_days_json, max_attempts) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("campaign_a", "tenant_a", "10:00", "19:00", '["mon","tue"]', 3),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO leads(campaign_id, lead_id, phone_ciphertext, phone_token, customer_name_ciphertext) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    ("campaign_a", "lead_1", pii.encrypt("+910000000001"), pii.phone_token("+910000000001"), pii.encrypt("One")),
                    ("campaign_a", "lead_2", pii.encrypt("+910000000002"), pii.phone_token("+910000000002"), pii.encrypt("Two")),
                ],
            )


def test_database_initializes_durable_schema(database):
    seed_minimum(database)
    with database.connect() as connection:
        tables = {
            row["table_name"]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        }
    assert {"tenants", "campaigns", "leads", "attempts", "phone_guards", "dnd_phones", "webhook_events", "app_meta"} <= tables


def test_active_phone_unique_constraint_prevents_cross_campaign_double_dial(database):
    seed_minimum(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO attempts(campaign_id, lead_id, tenant_id, phone_ciphertext, phone_token, attempt_no, request_id, state, "
            "previous_lead_state, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("campaign_a", "lead_1", "tenant_a", PiiProtector.from_env().encrypt("+910000000001"), PiiProtector.from_env().phone_token("+910000000001"), 1, "rid-1", "accepted", "pending", NOW, NOW),
        )
    with pytest.raises(UniqueViolation):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO attempts(campaign_id, lead_id, tenant_id, phone_ciphertext, phone_token, attempt_no, request_id, state, "
                "previous_lead_state, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("campaign_a", "lead_2", "tenant_a", PiiProtector.from_env().encrypt("+910000000001"), PiiProtector.from_env().phone_token("+910000000001"), 1, "rid-2", "prepared", "pending", NOW, NOW),
            )


def test_retryable_attempt_does_not_hold_active_phone_reservation(database):
    seed_minimum(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO attempts(campaign_id, lead_id, tenant_id, phone_ciphertext, phone_token, attempt_no, request_id, state, "
            "previous_lead_state, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("campaign_a", "lead_1", "tenant_a", PiiProtector.from_env().encrypt("+910000000001"), PiiProtector.from_env().phone_token("+910000000001"), 1, "rid-1", "retryable", "pending", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO attempts(campaign_id, lead_id, tenant_id, phone_ciphertext, phone_token, attempt_no, request_id, state, "
            "previous_lead_state, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("campaign_a", "lead_2", "tenant_a", PiiProtector.from_env().encrypt("+910000000001"), PiiProtector.from_env().phone_token("+910000000001"), 1, "rid-2", "prepared", "pending", NOW, NOW),
        )


def test_empty_campaign_days_are_persisted_as_empty_not_unrestricted(tmp_path, database):
    import json
    from app.config import AppConfig
    from app.repository import Repository

    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "leads_a.csv").write_text(
        "lead_id,phone,customer_name\nlead_1,+919000000001,One\n", encoding="utf-8"
    )
    raw = {
        "timezone": "Asia/Kolkata",
        "window_grace_seconds": 120,
        "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
        "webhook_secret": "secret",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "tenants": [{"tenant_id": "tenant_a", "max_concurrent_calls": 1}],
        "campaigns": [{
            "campaign_id": "campaign_a", "tenant_id": "tenant_a", "leads_file": "leads_a.csv",
            "window": {"start": "10:00", "end": "19:00", "days": []},
            "max_attempts": 3,
        }],
    }
    fixture_path = fixture_dir / "campaigns.json"
    fixture_path.write_text(json.dumps(raw), encoding="utf-8")
    config = AppConfig.load(fixture_path)
    Repository(database, config).initialize()
    with database.connect() as connection:
        value = connection.execute(
            "SELECT window_days_json FROM campaigns WHERE campaign_id = %s", ("campaign_a",)
        ).fetchone()["window_days_json"]
    assert value == "[]"


def test_only_one_scheduler_lock_owner_per_schema(database):
    second = Database(database.dsn, schema=database.schema)
    try:
        assert database.try_acquire_dispatcher_lock() is True
        assert second.try_acquire_dispatcher_lock() is False
        database.release_dispatcher_lock()
        assert second.try_acquire_dispatcher_lock() is True
    finally:
        second.close()
