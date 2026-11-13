import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.repository import Repository
from app.webhooks import signature_for
from helpers import completed_result, make_config
from datetime import datetime


def test_webhook_endpoint_verifies_hmac_and_deduplicates_delivery(tmp_path, database):
    config = make_config(tmp_path, campaign_count=1)
    repo = Repository(database, config)
    repo.initialize()
    now = datetime.fromisoformat("2026-11-12T17:30:00+05:30")
    candidate = repo.get_dispatch_candidates(now)[0]
    attempt = repo.reserve_attempt(candidate, now)
    result = completed_result(attempt, disposition="interested")
    raw = json.dumps(result, separators=(",", ":")).encode()
    app = create_app(
        config=config,
        database_url=database.dsn,
        database_schema=database.schema,
        provider_url="http://127.0.0.1:1",
        callback_url="http://127.0.0.1:8200/webhooks/telephony",
        output_path=tmp_path / "final_state.csv",
    )
    headers = {
        "X-AU-Signature": signature_for(raw, config.webhook_secret),
        "X-AU-Delivery-Id": "delivery-api-test",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        first = client.post("/webhooks/telephony", content=raw, headers=headers)
        duplicate = client.post("/webhooks/telephony", content=raw, headers=headers)
        invalid = client.post(
            "/webhooks/telephony",
            content=raw,
            headers={**headers, "X-AU-Signature": "0" * 64, "X-AU-Delivery-Id": "bad-signature"},
        )
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert invalid.status_code == 401


def test_health_endpoint_checks_database(tmp_path, database):
    config = make_config(tmp_path, campaign_count=1)
    app = create_app(
        config=config,
        database_url=database.dsn,
        database_schema=database.schema,
        provider_url="http://127.0.0.1:1",
        callback_url="http://127.0.0.1:8200/webhooks/telephony",
        output_path=tmp_path / "final_state.csv",
    )
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "up"}
