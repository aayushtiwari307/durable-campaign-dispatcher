import json

from app.config import AppConfig


def test_loads_campaigns_and_all_lead_fixtures(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "leads_a.csv").write_text(
        "lead_id,phone,customer_name\nlead_1,+919000000001,Test User\n",
        encoding="utf-8",
    )
    raw = {
        "timezone": "Asia/Kolkata",
        "window_grace_seconds": 120,
        "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
        "webhook_secret": "secret",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "tenants": [{"tenant_id": "tenant_a", "max_concurrent_calls": 2}],
        "campaigns": [{
            "campaign_id": "campaign_a", "tenant_id": "tenant_a", "leads_file": "leads_a.csv",
            "window": {"start": "10:00", "end": "19:00", "days": ["mon", "tue"]},
            "max_attempts": 3,
        }],
    }
    (fixture_dir / "campaigns.json").write_text(json.dumps(raw), encoding="utf-8")
    config = AppConfig.load(fixture_dir / "campaigns.json")
    assert len(config.campaigns) == 1
    assert len(config.leads) == 1
    assert config.leads[0].lead_id == "lead_1"
    assert config.campaigns[0].max_attempts == 3


def test_lead_identity_and_phone_are_preserved_exactly_as_provider_fixture(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "leads_a.csv").write_text(
        "lead_id,phone,customer_name\n lead_1 , +919000000001 ,Test User\n",
        encoding="utf-8",
    )
    raw = {
        "timezone": "Asia/Kolkata",
        "window_grace_seconds": 120,
        "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
        "webhook_secret": "secret",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "tenants": [{"tenant_id": "tenant_a", "max_concurrent_calls": 2}],
        "campaigns": [{
            "campaign_id": "campaign_a", "tenant_id": "tenant_a", "leads_file": "leads_a.csv",
            "window": {"start": "10:00", "end": "19:00", "days": ["mon", "tue"]},
            "max_attempts": 3,
        }],
    }
    (fixture_dir / "campaigns.json").write_text(json.dumps(raw), encoding="utf-8")
    config = AppConfig.load(fixture_dir / "campaigns.json")
    assert config.leads[0].lead_id == " lead_1 "
    assert config.leads[0].phone == " +919000000001 "


def test_missing_optional_customer_name_is_loaded_as_empty_string(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "leads_a.csv").write_text(
        "lead_id,phone,customer_name\nlead_1,+919000000001,\nlead_2,+919000000002\n",
        encoding="utf-8",
    )
    raw = {
        "timezone": "Asia/Kolkata",
        "window_grace_seconds": 120,
        "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
        "webhook_secret": "secret",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "tenants": [{"tenant_id": "tenant_a", "max_concurrent_calls": 2}],
        "campaigns": [{
            "campaign_id": "campaign_a", "tenant_id": "tenant_a", "leads_file": "leads_a.csv",
            "window": {"start": "10:00", "end": "19:00", "days": ["mon", "tue"]},
            "max_attempts": 3,
        }],
    }
    (fixture_dir / "campaigns.json").write_text(json.dumps(raw), encoding="utf-8")
    config = AppConfig.load(fixture_dir / "campaigns.json")
    assert [lead.customer_name for lead in config.leads] == ["", ""]


def test_duplicate_tenant_ids_are_rejected(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "leads_a.csv").write_text(
        "lead_id,phone,customer_name\nlead_1,+919000000001,User\n", encoding="utf-8"
    )
    raw = {
        "timezone": "Asia/Kolkata",
        "window_grace_seconds": 120,
        "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
        "webhook_secret": "secret",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "tenants": [
            {"tenant_id": "tenant_a", "max_concurrent_calls": 2},
            {"tenant_id": "tenant_a", "max_concurrent_calls": 2},
        ],
        "campaigns": [{
            "campaign_id": "campaign_a", "tenant_id": "tenant_a", "leads_file": "leads_a.csv",
            "window": {"start": "10:00", "end": "19:00", "days": ["mon", "tue"]},
            "max_attempts": 3,
        }],
    }
    (fixture_dir / "campaigns.json").write_text(json.dumps(raw), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="Duplicate tenant_id"):
        AppConfig.load(fixture_dir / "campaigns.json")


def test_provider_window_grace_defaults_to_documented_120_seconds(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "leads_a.csv").write_text(
        "lead_id,phone,customer_name\nlead_1,+919000000001,User\n", encoding="utf-8"
    )
    raw = {
        "timezone": "Asia/Kolkata",
        "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
        "webhook_secret": "secret",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "tenants": [{"tenant_id": "tenant_a", "max_concurrent_calls": 1}],
        "campaigns": [{
            "campaign_id": "campaign_a", "tenant_id": "tenant_a", "leads_file": "leads_a.csv",
            "window": {"start": "10:00", "end": "19:00", "days": ["mon", "tue"]},
            "max_attempts": 3,
        }],
    }
    path = fixture_dir / "campaigns.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert AppConfig.load(path).window_grace_seconds == 120
