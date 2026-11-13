from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .domain import CallingWindow


@dataclass(frozen=True, slots=True)
class TenantConfig:
    tenant_id: str
    max_concurrent_calls: int


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    campaign_id: str
    tenant_id: str
    leads_file: str
    window: CallingWindow
    max_attempts: int


@dataclass(frozen=True, slots=True)
class LeadConfig:
    campaign_id: str
    lead_id: str
    phone: str
    customer_name: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    root: Path
    fixture_path: Path
    timezone: str
    regulatory_window: CallingWindow
    window_grace_seconds: int
    retry_policy: dict[str, int]
    webhook_secret: str
    tenants: tuple[TenantConfig, ...]
    campaigns: tuple[CampaignConfig, ...]
    leads: tuple[LeadConfig, ...]

    @classmethod
    def load(cls, fixture_path: str | Path) -> "AppConfig":
        path = Path(fixture_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Campaign fixture not found: {path}")
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        base = path.parent

        tenants = tuple(
            TenantConfig(
                tenant_id=item["tenant_id"],
                max_concurrent_calls=int(item["max_concurrent_calls"]),
            )
            for item in raw["tenants"]
        )
        if any(not item.tenant_id.strip() for item in tenants):
            raise ValueError("Tenant IDs must be non-empty")
        if any(item.max_concurrent_calls < 1 for item in tenants):
            raise ValueError("Tenant max_concurrent_calls must be positive")
        tenant_ids = {item.tenant_id for item in tenants}
        if len(tenant_ids) != len(tenants):
            raise ValueError("Duplicate tenant_id in campaigns configuration")
        campaigns: list[CampaignConfig] = []
        leads: list[LeadConfig] = []
        seen_campaigns: set[str] = set()
        seen_leads: set[tuple[str, str]] = set()

        for item in raw["campaigns"]:
            campaign_id = item["campaign_id"]
            if campaign_id in seen_campaigns:
                raise ValueError(f"Duplicate campaign_id: {campaign_id}")
            if item["tenant_id"] not in tenant_ids:
                raise ValueError(f"Unknown tenant for campaign {campaign_id}")
            max_attempts = int(item["max_attempts"])
            if max_attempts < 1:
                raise ValueError(f"max_attempts must be positive: {campaign_id}")
            campaign = CampaignConfig(
                campaign_id=campaign_id,
                tenant_id=item["tenant_id"],
                leads_file=item["leads_file"],
                window=CallingWindow.from_config(item["window"]),
                max_attempts=max_attempts,
            )
            campaigns.append(campaign)
            seen_campaigns.add(campaign_id)
            leads_path = (base / campaign.leads_file).resolve()
            if leads_path.parent != base.resolve():
                raise ValueError(f"Lead fixture must be inside {base}")
            if not leads_path.is_file():
                raise FileNotFoundError(f"Lead fixture not found: {leads_path}")
            with leads_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    lead_id = row.get("lead_id") or ""
                    phone = row.get("phone") or ""
                    key = (campaign_id, lead_id)
                    if not lead_id.strip() or not phone.strip():
                        raise ValueError(f"Missing lead_id/phone in {leads_path}")
                    if key in seen_leads:
                        raise ValueError(f"Duplicate lead identity: {key}")
                    seen_leads.add(key)
                    leads.append(
                        LeadConfig(
                            campaign_id=campaign_id,
                            lead_id=lead_id,
                            phone=phone,
                            customer_name=(row.get("customer_name") or "").strip(),
                        )
                    )

        window_grace_seconds = int(raw.get("window_grace_seconds", 120))
        if window_grace_seconds < 0:
            raise ValueError("window_grace_seconds must be non-negative")
        timezone = raw.get("timezone", "Asia/Kolkata")
        if timezone != "Asia/Kolkata":
            raise ValueError("Assignment B scheduling timezone must be Asia/Kolkata")

        expected_retry_keys = {"no_answer_minutes", "busy_minutes", "failed_minutes"}
        raw_retry_policy = raw["retry_policy"]
        if set(raw_retry_policy) != expected_retry_keys:
            raise ValueError("retry_policy must define no_answer_minutes, busy_minutes and failed_minutes")
        retry_policy = {
            key.removesuffix("_minutes"): int(value)
            for key, value in raw_retry_policy.items()
        }
        if any(value < 0 for value in retry_policy.values()):
            raise ValueError("retry delays must be non-negative")

        webhook_secret = raw.get("webhook_secret")
        if not isinstance(webhook_secret, str) or not webhook_secret:
            raise ValueError("webhook_secret must be a non-empty string")

        return cls(
            root=path.parent.parent.parent,
            fixture_path=path,
            timezone=timezone,
            regulatory_window=CallingWindow.from_config(raw["regulatory_window"]),
            window_grace_seconds=window_grace_seconds,
            retry_policy=retry_policy,
            webhook_secret=webhook_secret,
            tenants=tenants,
            campaigns=tuple(campaigns),
            leads=tuple(leads),
        )
