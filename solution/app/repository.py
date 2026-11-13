from __future__ import annotations

import csv
import hashlib
import json
import random
import time
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, CampaignConfig
from .database import Database
from .pii import PiiProtector
from psycopg.errors import DeadlockDetected, SerializationFailure, UniqueViolation
from psycopg.types.json import Jsonb
from .domain import (
    CampaignSchedule,
    LeadState,
    derive_final_state,
    next_eligible_in_window,
    retry_not_before,
    sort_dispatch_candidates,
)

ACTIVE_ATTEMPT_STATES = ("prepared", "requesting", "unknown", "accepted")
ACCEPTED_ATTEMPT_STATES = ("accepted", "ended")


def iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Persisted datetimes must be timezone-aware")
    return value.isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def stable_request_id(campaign_id: str, lead_id: str, attempt_no: int) -> str:
    material = f"{campaign_id}\0{lead_id}\0{attempt_no}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:32]
    return f"au_{digest}"


def retry_transaction_conflicts(function):
    """Retry a whole repository operation after a PostgreSQL deadlock/serialization abort.

    The transaction context has rolled back before the exception reaches this
    wrapper, so each retry starts from a fresh transaction and re-reads durable
    state. Provider calls are never made inside these decorated operations.
    """
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        for retry in range(4):
            try:
                return function(self, *args, **kwargs)
            except (DeadlockDetected, SerializationFailure):
                if retry == 3:
                    raise
                delay = min(0.20, 0.02 * (2 ** retry) + random.uniform(0.0, 0.02))
                time.sleep(delay)
        raise RuntimeError("Unreachable transaction retry state")

    return wrapped


class Repository:
    def __init__(
        self,
        database: Database,
        config: AppConfig,
        pii: PiiProtector | None = None,
    ):
        self.db = database
        self.config = config
        self.pii = pii or PiiProtector.from_env()
        self.campaigns = {item.campaign_id: item for item in config.campaigns}
        self.schedules = {
            item.campaign_id: CampaignSchedule(
                item.window, config.regulatory_window, config.window_grace_seconds
            )
            for item in config.campaigns
        }

    def _fixture_fingerprint(self) -> str:
        """Hash the complete fixture set so a reused DB cannot mix grading seeds."""
        digest = hashlib.sha256()
        paths = [self.config.fixture_path]
        paths.extend(
            (self.config.fixture_path.parent / campaign.leads_file).resolve()
            for campaign in self.config.campaigns
        )
        for path in sorted(set(paths), key=lambda item: item.as_posix()):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def initialize(self) -> None:
        self.db.initialize()
        fingerprint = self._fixture_fingerprint()
        with self.db.transaction() as connection:
            # Serialize seed initialization across processes. In particular, a second
            # process using a different fixture seed must not pass a stale pre-lock
            # fingerprint check and mix rows into the first process's database.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('campaign_dispatcher_seed'))"
            )
            stored = connection.execute(
                "SELECT value FROM app_meta WHERE key = %s", ("fixture_fingerprint",)
            ).fetchone()
            if stored is not None and stored["value"] != fingerprint:
                raise ValueError(
                    "The database was initialized with a different fixture seed. "
                    "Use a fresh PostgreSQL database/schema for a new seed."
                )
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                ("fixture_fingerprint", fingerprint),
            )
            for tenant in self.config.tenants:
                row = connection.execute(
                    "SELECT max_concurrent_calls FROM tenants WHERE tenant_id = %s",
                    (tenant.tenant_id,),
                ).fetchone()
                if row is not None and row["max_concurrent_calls"] != tenant.max_concurrent_calls:
                    raise ValueError(f"Tenant configuration changed for {tenant.tenant_id}; use a fresh state DB")
                connection.execute(
                    "INSERT INTO tenants(tenant_id, max_concurrent_calls) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (tenant.tenant_id, tenant.max_concurrent_calls),
                )

            for campaign in self.config.campaigns:
                days_json = json.dumps(sorted(campaign.window.days)) if campaign.window.days is not None else None
                row = connection.execute(
                    "SELECT tenant_id, window_start, window_end, window_days_json, max_attempts "
                    "FROM campaigns WHERE campaign_id = %s",
                    (campaign.campaign_id,),
                ).fetchone()
                expected = (
                    campaign.tenant_id,
                    campaign.window.start.isoformat(timespec="minutes"),
                    campaign.window.end.isoformat(timespec="minutes"),
                    days_json,
                    campaign.max_attempts,
                )
                if row is not None and tuple(row[key] for key in ("tenant_id", "window_start", "window_end", "window_days_json", "max_attempts")) != expected:
                    raise ValueError(f"Campaign configuration changed for {campaign.campaign_id}; use a fresh state DB")
                connection.execute(
                    "INSERT INTO campaigns(campaign_id, tenant_id, window_start, window_end, "
                    "window_days_json, max_attempts) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (campaign.campaign_id, *expected),
                )

            for lead in self.config.leads:
                row = connection.execute(
                    "SELECT phone_token FROM leads WHERE campaign_id = %s AND lead_id = %s",
                    (lead.campaign_id, lead.lead_id),
                ).fetchone()
                phone_token = self.pii.phone_token(lead.phone)
                if row is not None and row["phone_token"] != phone_token:
                    raise ValueError(
                        f"Phone fixture changed for {lead.campaign_id}/{lead.lead_id}; use a fresh state DB"
                    )
                connection.execute(
                    "INSERT INTO leads(campaign_id, lead_id, phone_ciphertext, phone_token, customer_name_ciphertext) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        lead.campaign_id,
                        lead.lead_id,
                        self.pii.encrypt(lead.phone),
                        phone_token,
                        self.pii.encrypt(lead.customer_name),
                    ),
                )

    def get_dispatch_candidates(self, now: datetime) -> list[dict[str, Any]]:
        """Return only currently due candidates, already ordered by R6 priority."""
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT campaign_id, lead_id, phone_ciphertext, state, next_eligible_at, callback_at "
                "FROM leads WHERE state IN ('pending', 'retry_wait', 'callback_wait')"
            ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            state = row["state"]
            if state == LeadState.CALLBACK_WAIT:
                not_before = parse_datetime(row["callback_at"])
            elif state == LeadState.RETRY_WAIT:
                not_before = parse_datetime(row["next_eligible_at"])
            else:
                not_before = now
            if not_before is None:
                raise RuntimeError(
                    f"Lead {row['campaign_id']}/{row['lead_id']} is {state} without an eligible timestamp"
                )
            eligible_at = next_eligible_in_window(not_before, self.schedules[row["campaign_id"]])
            if eligible_at <= now:
                candidates.append(
                    {
                        "campaign_id": row["campaign_id"],
                        "lead_id": row["lead_id"],
                        "phone": self.pii.decrypt(row["phone_ciphertext"]),
                        "state": state,
                        "eligible_at": eligible_at,
                    }
                )
        return sort_dispatch_candidates(candidates)

    @retry_transaction_conflicts
    def reserve_attempt(self, candidate: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """Atomically reserve a tenant slot and phone before any provider request."""
        cid = candidate["campaign_id"]
        lid = candidate["lead_id"]
        campaign: CampaignConfig = self.campaigns[cid]
        tenant_id = campaign.tenant_id
        iso(now)  # Validate timezone-awareness before binding native TIMESTAMPTZ values.
        now_db = now
        now_real = time.time()
        phone_token = self.pii.phone_token(candidate["phone"])
        try:
            with self.db.transaction() as connection:
                # Serialize reservations and DND result processing for this phone.
                # Lock phone first everywhere, then lead/attempt rows, to avoid a
                # race where a reservation observes no DND row just before a DND
                # webhook commits and frees the provider's active-phone index.
                connection.execute(
                    "INSERT INTO phone_guards(phone_token) VALUES (%s) ON CONFLICT DO NOTHING",
                    (phone_token,),
                )
                connection.execute(
                    "SELECT phone_token FROM phone_guards WHERE phone_token = %s FOR UPDATE",
                    (phone_token,),
                ).fetchone()
                lead = connection.execute(
                    "SELECT * FROM leads WHERE campaign_id = %s AND lead_id = %s FOR UPDATE",
                    (cid, lid),
                ).fetchone()
                if lead is None or lead["state"] not in ("pending", "retry_wait", "callback_wait"):
                    return None
                if lead["phone_token"] != phone_token:
                    raise RuntimeError("Candidate phone does not match persisted lead")
                is_dnd = connection.execute(
                    "SELECT 1 FROM dnd_phones WHERE phone_token = %s", (phone_token,)
                ).fetchone() is not None
                if is_dnd:
                    connection.execute(
                        "UPDATE leads SET state = 'dnd_blocked', updated_at = %s WHERE campaign_id = %s AND lead_id = %s",
                        (now_db, cid, lid),
                    )
                    return None
                tenant = connection.execute(
                    "SELECT max_concurrent_calls FROM tenants WHERE tenant_id = %s FOR UPDATE", (tenant_id,)
                ).fetchone()
                active_tenant = connection.execute(
                    "SELECT COUNT(*) AS count FROM attempts WHERE tenant_id = %s AND state IN (%s, %s, %s, %s)",
                    (tenant_id, *ACTIVE_ATTEMPT_STATES),
                ).fetchone()["count"]
                if active_tenant >= tenant["max_concurrent_calls"]:
                    return None
                active_phone = connection.execute(
                    "SELECT request_id FROM attempts WHERE phone_token = %s AND state IN (%s, %s, %s, %s) LIMIT 1",
                    (phone_token, *ACTIVE_ATTEMPT_STATES),
                ).fetchone()
                if active_phone is not None:
                    return None
                accepted_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM attempts WHERE campaign_id = %s AND lead_id = %s AND state IN (%s, %s)",
                    (cid, lid, *ACCEPTED_ATTEMPT_STATES),
                ).fetchone()["count"]
                if accepted_count >= campaign.max_attempts:
                    connection.execute(
                        "UPDATE leads SET state = 'exhausted', updated_at = %s WHERE campaign_id = %s AND lead_id = %s",
                        (now_db, cid, lid),
                    )
                    return None
                attempt_no = accepted_count + 1
                existing = connection.execute(
                    "SELECT * FROM attempts WHERE campaign_id = %s AND lead_id = %s AND attempt_no = %s",
                    (cid, lid, attempt_no),
                ).fetchone()
                if existing is not None:
                    if existing["state"] != "retryable":
                        return None
                    if existing["transport_retry_at"] and existing["transport_retry_at"] > now_real:
                        return None
                    request_id = existing["request_id"]
                    connection.execute(
                        "UPDATE attempts SET state = 'requesting', previous_lead_state = %s, "
                        "updated_at = %s, updated_real_at = %s, transport_retry_at = NULL, last_error = NULL "
                        "WHERE attempt_id = %s",
                        (lead["state"], now_db, now_real, existing["attempt_id"]),
                    )
                else:
                    request_id = stable_request_id(cid, lid, attempt_no)
                    connection.execute(
                        "INSERT INTO attempts(campaign_id, lead_id, tenant_id, phone_ciphertext, phone_token, "
                        "attempt_no, request_id, state, previous_lead_state, created_at, updated_at, updated_real_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'requesting', %s, %s, %s, %s)",
                        (cid, lid, tenant_id, self.pii.encrypt(candidate["phone"]), phone_token,
                         attempt_no, request_id, lead["state"], now_db, now_db, now_real),
                    )
                connection.execute(
                    "UPDATE leads SET state = 'dialing', updated_at = %s WHERE campaign_id = %s AND lead_id = %s",
                    (now_db, cid, lid),
                )
                row = connection.execute(
                    "SELECT * FROM attempts WHERE request_id = %s", (request_id,)
                ).fetchone()
                return self._attempt_view(row)
        except UniqueViolation as exc:
            # The partial unique index is the final guard against a cross-campaign race.
            if getattr(getattr(exc, "diag", None), "constraint_name", None) == "uq_active_phone":
                return None
            raise

    def _attempt_view(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        ciphertext = item.pop("phone_ciphertext", None)
        if ciphertext is not None:
            item["phone"] = self.pii.decrypt(ciphertext)
        return item

    def _lock_attempt_for_transition(self, connection, request_id: str) -> dict[str, Any] | None:
        """Lock the phone guard before the attempt row for every transition.

        Reservations and result processing use this same per-phone serialization
        point. Keeping one lock order prevents deadlocks between an attempt transition
        and a concurrent reservation for the same phone.
        """
        identity = connection.execute(
            "SELECT phone_token FROM attempts WHERE request_id = %s", (request_id,)
        ).fetchone()
        if identity is None:
            return None
        phone_token = identity["phone_token"]
        connection.execute(
            "INSERT INTO phone_guards(phone_token) VALUES (%s) ON CONFLICT DO NOTHING",
            (phone_token,),
        )
        connection.execute(
            "SELECT phone_token FROM phone_guards WHERE phone_token = %s FOR UPDATE",
            (phone_token,),
        ).fetchone()
        return connection.execute(
            "SELECT * FROM attempts WHERE request_id = %s FOR UPDATE", (request_id,)
        ).fetchone()

    @retry_transaction_conflicts
    def mark_unknown(self, request_id: str, now: datetime, detail: str) -> None:
        iso(now)
        with self.db.transaction() as connection:
            attempt = self._lock_attempt_for_transition(connection, request_id)
            if attempt is None:
                return
            if attempt["state"] not in ("requesting", "unknown"):
                return
            connection.execute(
                "UPDATE attempts SET state = 'unknown', updated_at = %s, updated_real_at = %s, "
                "last_error = %s WHERE request_id = %s AND state IN ('requesting', 'unknown')",
                (now, time.time(), detail[:1000], request_id),
            )

    @retry_transaction_conflicts
    def mark_retryable(self, request_id: str, now: datetime, detail: str, delay_seconds: float = 0.25) -> None:
        iso(now)
        with self.db.transaction() as connection:
            attempt = self._lock_attempt_for_transition(connection, request_id)
            if (
                attempt is None
                or attempt["state"] not in ("requesting", "unknown", "retryable")
                or attempt["call_sid"] is not None
            ):
                # A delayed 503/timeout handler must never overwrite a newer accepted
                # attempt or release its active-phone/tenant reservation.
                return
            real_now = time.time()
            connection.execute(
                "UPDATE attempts SET state = 'retryable', updated_at = %s, updated_real_at = %s, "
                "transport_retry_at = %s, last_error = %s WHERE request_id = %s",
                (now, real_now, real_now + max(0.0, delay_seconds), detail[:1000], request_id),
            )
            connection.execute(
                "UPDATE leads SET state = %s, updated_at = %s WHERE campaign_id = %s AND lead_id = %s AND state = 'dialing'",
                (attempt["previous_lead_state"], now, attempt["campaign_id"], attempt["lead_id"]),
            )

    @retry_transaction_conflicts
    def mark_accepted(self, request_id: str, call_sid: str, now: datetime) -> None:
        iso(now)
        with self.db.transaction() as connection:
            attempt = self._lock_attempt_for_transition(connection, request_id)
            if attempt is None:
                raise KeyError(f"Unknown request_id from provider: {request_id}")
            if attempt["call_sid"] and attempt["call_sid"] != call_sid:
                raise ValueError(f"Provider returned conflicting call_sid for request_id {request_id}")
            if attempt["state"] != "ended":
                connection.execute(
                    "UPDATE attempts SET state = 'accepted', call_sid = %s, accepted_at = COALESCE(accepted_at, %s), "
                    "updated_at = %s, updated_real_at = %s, transport_retry_at = NULL "
                    "WHERE request_id = %s",
                    (call_sid, now, now, time.time(), request_id),
                )
                connection.execute(
                    "UPDATE leads SET state = 'dialing', updated_at = %s "
                    "WHERE campaign_id = %s AND lead_id = %s AND state IN ('pending', 'retry_wait', 'callback_wait')",
                    (now, attempt["campaign_id"], attempt["lead_id"]),
                )
            elif not attempt["call_sid"]:
                connection.execute(
                    "UPDATE attempts SET call_sid = %s WHERE request_id = %s", (call_sid, request_id)
                )

    def _apply_result(self, connection, result: dict[str, Any]) -> bool:
        request_id = result["request_id"]
        phone_token = self.pii.phone_token(result["phone"])
        # Lock the same per-phone guard used by reservations before releasing an
        # accepted attempt or recording global DND. This makes DND and a new dial
        # mutually ordered across every campaign.
        connection.execute(
            "INSERT INTO phone_guards(phone_token) VALUES (%s) ON CONFLICT DO NOTHING",
            (phone_token,),
        )
        connection.execute(
            "SELECT phone_token FROM phone_guards WHERE phone_token = %s FOR UPDATE",
            (phone_token,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT * FROM attempts WHERE request_id = %s FOR UPDATE", (request_id,)
        ).fetchone()
        if attempt is None:
            raise KeyError(f"Webhook/reconciliation references unknown request_id: {request_id}")
        expected = (attempt["campaign_id"], attempt["lead_id"], attempt["phone_token"], attempt["tenant_id"])
        received = (result["campaign_id"], result["lead_id"], phone_token, result["tenant_id"])
        if expected != received:
            raise ValueError(f"Provider result identity mismatch for request_id {request_id}")
        call_sid = result.get("call_sid")
        if not isinstance(call_sid, str) or not call_sid.strip():
            raise ValueError(f"Provider result has an invalid call_sid for request_id {request_id}")
        if attempt["call_sid"] and attempt["call_sid"] != call_sid:
            raise ValueError(f"Provider returned conflicting call_sid for request_id {request_id}")
        if attempt["state"] == "ended":
            # An exact replay is harmless, but a second call_sid for the same
            # idempotency key is a provider-integrity error and must not be hidden.
            return False
        if attempt["state"] not in {"requesting", "unknown", "accepted"}:
            raise ValueError(
                f"Provider result arrived for non-active request_id {request_id} "
                f"(state={attempt['state']!r})"
            )

        if result.get("status") not in {"completed", "no_answer", "busy", "failed"}:
            raise ValueError(f"Unknown provider status for request_id {request_id}: {result.get('status')!r}")
        ended_at = parse_datetime(result["ended_at"])
        if ended_at is None:
            raise ValueError("Provider result is missing ended_at")
        iso(ended_at)  # Reject naive provider timestamps before database persistence.
        started_at = parse_datetime(result.get("started_at")) or ended_at
        iso(started_at)
        status = result["status"]
        disposition = result.get("disposition")
        callback_value = result.get("callback_at")
        callback_at = parse_datetime(callback_value) if callback_value else None
        if disposition == "callback_requested" and callback_at is None:
            raise ValueError("callback_requested result is missing callback_at")
        if callback_at is not None:
            iso(callback_at)

        # Persist only the provider's documented non-PII result fields. The phone
        # is already represented by encrypted ciphertext + keyed lookup token; an
        # allowlist prevents future provider payload additions from leaking PII.
        safe_result = {
            key: result.get(key)
            for key in (
                "event", "call_sid", "request_id", "tenant_id", "campaign_id",
                "lead_id", "status", "disposition", "callback_at",
                "started_at", "ended_at", "duration_sec",
            )
        }
        connection.execute(
            "UPDATE attempts SET state = 'ended', call_sid = COALESCE(call_sid, %s), "
            "accepted_at = COALESCE(accepted_at, %s), ended_at = %s, status = %s, disposition = %s, "
            "callback_at = %s, result_json = %s, updated_at = %s, updated_real_at = %s, "
            "transport_retry_at = NULL WHERE request_id = %s",
            (result["call_sid"], started_at, ended_at, status,
             disposition, callback_at, Jsonb(safe_result), ended_at, time.time(), request_id),
        )
        campaign = self.campaigns[attempt["campaign_id"]]
        accepted_count = connection.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE campaign_id = %s AND lead_id = %s AND state IN (%s, %s)",
            (attempt["campaign_id"], attempt["lead_id"], *ACCEPTED_ATTEMPT_STATES),
        ).fetchone()["count"]

        if disposition == "dnd":
            connection.execute(
                "INSERT INTO dnd_phones(phone_token, first_seen_at, source_request_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (phone_token, ended_at, request_id),
            )

        final_state = derive_final_state(
            terminal_disposition=disposition,
            accepted_attempts=accepted_count,
            max_attempts=campaign.max_attempts,
            phone_is_dnd=disposition == "dnd",
            has_active_call=False,
        )
        if final_state is not None:
            state = str(final_state)
            next_eligible = None
        elif connection.execute(
            "SELECT 1 FROM dnd_phones WHERE phone_token = %s", (phone_token,)
        ).fetchone() is not None:
            state = str(LeadState.DND_BLOCKED)
            next_eligible = None
        elif status in self.config.retry_policy:
            eligible = retry_not_before(status, ended_at, self.config.retry_policy)
            state = str(LeadState.RETRY_WAIT)
            next_eligible = eligible
        elif disposition == "callback_requested":
            state = str(LeadState.CALLBACK_WAIT)
            next_eligible = None
        else:
            # Defensive: every non-terminal disposition must have an explicit next action.
            raise ValueError(f"Unrecognised non-terminal provider result: {status}/{disposition}")

        connection.execute(
            "UPDATE leads SET state = %s, next_eligible_at = %s, callback_at = %s, last_status = %s, "
            "last_disposition = %s, last_ended_at = %s, updated_at = %s WHERE campaign_id = %s AND lead_id = %s",
            (state, next_eligible, callback_at if state == LeadState.CALLBACK_WAIT else None,
             status, disposition, ended_at, ended_at, attempt["campaign_id"], attempt["lead_id"]),
        )

        if disposition == "dnd":
            # Preserve provider precedence: completed > exhausted > dnd_blocked.
            connection.execute(
                "UPDATE leads SET state = 'dnd_blocked', next_eligible_at = NULL, callback_at = NULL, updated_at = %s "
                "WHERE phone_token = %s AND NOT (campaign_id = %s AND lead_id = %s) "
                "AND state NOT IN ('completed', 'exhausted', 'dnd_blocked')",
                (ended_at, phone_token, attempt["campaign_id"], attempt["lead_id"]),
            )
        return True

    @retry_transaction_conflicts
    def apply_provider_result(self, result: dict[str, Any]) -> bool:
        with self.db.transaction() as connection:
            return self._apply_result(connection, result)

    @retry_transaction_conflicts
    def apply_webhook(
        self,
        *,
        delivery_id: str,
        raw_body: bytes,
        result: dict[str, Any],
        received_at: datetime,
    ) -> bool:
        iso(received_at)
        digest = hashlib.sha256(raw_body).hexdigest()
        with self.db.transaction() as connection:
            # Preserve the global lock order: phone guard before any operation
            # that can acquire a foreign-key lock on the attempts row.
            phone_token = self.pii.phone_token(result["phone"])
            connection.execute(
                "INSERT INTO phone_guards(phone_token) VALUES (%s) ON CONFLICT DO NOTHING",
                (phone_token,),
            )
            connection.execute(
                "SELECT phone_token FROM phone_guards WHERE phone_token = %s FOR UPDATE",
                (phone_token,),
            ).fetchone()

            inserted = connection.execute(
                "INSERT INTO webhook_events(delivery_id, request_id, body_sha256, received_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (delivery_id) DO NOTHING "
                "RETURNING delivery_id",
                (delivery_id, result["request_id"], digest, received_at),
            ).fetchone()
            if inserted is None:
                existing = connection.execute(
                    "SELECT body_sha256 FROM webhook_events WHERE delivery_id = %s",
                    (delivery_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("Webhook deduplication row disappeared during conflict handling")
                if existing["body_sha256"] != digest:
                    raise ValueError(f"Webhook delivery_id was reused with a different body: {delivery_id}")
                return False
            self._apply_result(connection, result)
            return True

    def active_attempts(self) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE state IN ('requesting', 'unknown', 'accepted') "
                "ORDER BY created_at, attempt_id"
            ).fetchall()
        return [self._attempt_view(row) for row in rows]

    def update_status_snapshot(self, now: datetime) -> dict[str, Any]:
        with self.db.connect() as connection:
            campaigns = connection.execute(
                "SELECT campaign_id, state, COUNT(*) AS count FROM leads GROUP BY campaign_id, state"
            ).fetchall()
            active_by_tenant = connection.execute(
                "SELECT tenant_id, COUNT(*) AS count FROM attempts WHERE state IN ('requesting','unknown','accepted') "
                "GROUP BY tenant_id"
            ).fetchall()
            counts = connection.execute(
                "SELECT state, COUNT(*) AS count FROM leads GROUP BY state"
            ).fetchall()
        campaign_progress: dict[str, dict[str, int]] = {}
        for row in campaigns:
            campaign_progress.setdefault(row["campaign_id"], {})[row["state"]] = row["count"]
        return {
            "sim_now": iso(now),
            "leads_by_state": {row["state"]: row["count"] for row in counts},
            "campaigns": campaign_progress,
            "active_calls_by_tenant": {row["tenant_id"]: row["count"] for row in active_by_tenant},
        }

    def all_leads_final(self) -> bool:
        with self.db.connect() as connection:
            incomplete = connection.execute(
                "SELECT COUNT(*) AS count FROM leads WHERE state NOT IN ('completed', 'exhausted', 'dnd_blocked')"
            ).fetchone()["count"]
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM attempts WHERE state IN ('prepared', 'requesting', 'unknown', 'accepted')"
            ).fetchone()["count"]
        return incomplete == 0 and active == 0

    def export_final_state(self, output_path: str | Path) -> Path:
        with self.db.connect() as connection:
            leads = connection.execute(
                "SELECT l.*, c.max_attempts FROM leads l JOIN campaigns c USING(campaign_id) "
                'ORDER BY l.campaign_id COLLATE "C", l.lead_id COLLATE "C"'
            ).fetchall()
            output_rows: list[dict[str, Any]] = []
            for lead in leads:
                attempts = connection.execute(
                    "SELECT state, status, disposition FROM attempts "
                    "WHERE campaign_id = %s AND lead_id = %s ORDER BY attempt_no DESC",
                    (lead["campaign_id"], lead["lead_id"]),
                ).fetchall()
                accepted_count = sum(row["state"] in ACCEPTED_ATTEMPT_STATES for row in attempts)
                last_ended = next((row for row in attempts if row["state"] == "ended"), None)
                active = any(row["state"] in ACTIVE_ATTEMPT_STATES for row in attempts)
                phone_is_dnd = connection.execute(
                    "SELECT 1 FROM dnd_phones WHERE phone_token = %s", (lead["phone_token"],)
                ).fetchone() is not None
                final_state = derive_final_state(
                    terminal_disposition=lead["last_disposition"],
                    accepted_attempts=accepted_count,
                    max_attempts=lead["max_attempts"],
                    phone_is_dnd=phone_is_dnd,
                    has_active_call=active,
                )
                if final_state is None:
                    raise RuntimeError(
                        f"Cannot export incomplete lead {lead['campaign_id']}/{lead['lead_id']}"
                    )
                output_rows.append(
                    {
                        "campaign_id": lead["campaign_id"],
                        "lead_id": lead["lead_id"],
                        "final_state": str(final_state),
                        "attempts": accepted_count,
                        "last_status": last_ended["status"] if last_ended else "",
                        "last_disposition": last_ended["disposition"] if last_ended and last_ended["disposition"] else "",
                    }
                )

        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["campaign_id", "lead_id", "final_state", "attempts", "last_status", "last_disposition"],
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(output_rows)
            handle.flush()
        temp_path.replace(path)
        return path
