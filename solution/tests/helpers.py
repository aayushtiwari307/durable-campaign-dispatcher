import csv
import json
from pathlib import Path

from app.config import AppConfig


def make_config(base: Path, *, tenant_limit: int = 2, campaign_count: int = 2) -> AppConfig:
    fixture_dir = base / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    campaign_rows = []
    leads = {
        "leads_c1.csv": [
            ("lead_1", "+919000000001", "One"),
            ("lead_2", "+919000000002", "Two"),
        ],
        "leads_c2.csv": [
            ("lead_3", "+919000000001", "Three"),
        ],
    }
    for index in range(1, campaign_count + 1):
        campaign_rows.append({
            "campaign_id": f"campaign_{index}",
            "tenant_id": "tenant_a",
            "leads_file": f"leads_c{index}.csv",
            "window": {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
            "max_attempts": 3,
        })
        with (fixture_dir / f"leads_c{index}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["lead_id", "phone", "customer_name"])
            writer.writerows(leads[f"leads_c{index}.csv"])

    raw = {
        "sim_start": "2026-11-12T17:30:00+05:30",
        "timezone": "Asia/Kolkata",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "window_grace_seconds": 120,
        "retry_policy": {"no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15},
        "webhook_secret": "test-webhook-secret",
        "tenants": [{"tenant_id": "tenant_a", "max_concurrent_calls": tenant_limit}],
        "campaigns": campaign_rows,
    }
    path = fixture_dir / "campaigns.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AppConfig.load(path)


def completed_result(attempt: dict, *, disposition: str | None, status: str = "completed", ended_at: str = "2026-11-12T17:35:00+05:30") -> dict:
    callback_at = "2026-11-12T18:35:00+05:30" if disposition == "callback_requested" else None
    return {
        "event": "call.completed",
        "call_sid": "CA" + attempt["request_id"][-24:],
        "request_id": attempt["request_id"],
        "tenant_id": attempt["tenant_id"],
        "campaign_id": attempt["campaign_id"],
        "lead_id": attempt["lead_id"],
        "phone": attempt["phone"],
        "status": status,
        "disposition": disposition,
        "callback_at": callback_at,
        "started_at": "2026-11-12T17:30:00+05:30",
        "ended_at": ended_at,
        "duration_sec": 120,
    }
