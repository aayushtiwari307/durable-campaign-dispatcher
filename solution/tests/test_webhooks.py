from app.webhooks import signature_for, verify_signature


def test_webhook_signature_is_hmac_sha256_over_exact_raw_body():
    raw = b'{"event":"call.completed","request_id":"abc"}'
    signature = signature_for(raw, "secret")
    assert verify_signature(raw, "secret", signature)
    assert verify_signature(raw, "secret", signature.upper())
    assert not verify_signature(raw + b" ", "secret", signature)
    assert not verify_signature(raw, "wrong-secret", signature)
    assert not verify_signature(raw, "secret", None)


def test_malformed_non_ascii_signature_fails_closed_without_raising():
    raw = b'{"event":"call.completed"}'
    assert not verify_signature(raw, "secret", "é" * 64)
    assert not verify_signature(raw, "secret", "g" * 64)
    assert not verify_signature(raw, "secret", "0" * 63)


def test_unicode_signature_is_rejected_without_exception():
    from app.webhooks import verify_signature

    assert verify_signature(b"{}", "secret", "é" * 64) is False


def test_webhook_acquires_phone_guard_before_foreign_key_insert(tmp_path):
    import json
    from contextlib import contextmanager
    from datetime import datetime

    from app.repository import Repository
    from helpers import make_config

    class Cursor:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class RecordingConnection:
        def __init__(self):
            self.statements = []

        def execute(self, query, params=None):
            normalized = " ".join(str(query).split())
            self.statements.append(normalized)
            if "INSERT INTO webhook_events" in normalized:
                return Cursor({"delivery_id": params[0]})
            return Cursor(None)

    class RecordingDatabase:
        def __init__(self):
            self.connection = RecordingConnection()

        @contextmanager
        def transaction(self):
            yield self.connection

    config = make_config(tmp_path, campaign_count=1)
    fake_db = RecordingDatabase()
    repo = Repository(fake_db, config)
    repo._apply_result = lambda connection, result: True
    result = {"request_id": "rid-lock-order", "phone": "+919000000001"}
    raw = json.dumps(result, separators=(",", ":")).encode()

    assert repo.apply_webhook(
        delivery_id="delivery-lock-order",
        raw_body=raw,
        result=result,
        received_at=datetime.fromisoformat("2026-11-12T17:30:00+05:30"),
    ) is True

    statements = fake_db.connection.statements
    phone_insert = next(i for i, statement in enumerate(statements) if "INSERT INTO phone_guards" in statement)
    phone_lock = next(i for i, statement in enumerate(statements) if "FROM phone_guards" in statement and "FOR UPDATE" in statement)
    webhook_insert = next(i for i, statement in enumerate(statements) if "INSERT INTO webhook_events" in statement)
    assert phone_insert < phone_lock < webhook_insert
