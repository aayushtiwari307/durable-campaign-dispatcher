from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from .config import AppConfig
from .domain import candidate_priority, is_allowed_start
from .provider import ProviderClient, ProviderError, estimate_submission_time
from .repository import Repository

logger = logging.getLogger("campaign_dispatcher")


class ProviderViolation(RuntimeError):
    """The mock provider rejected a request because a business rule was violated."""


class Dispatcher:
    def __init__(
        self,
        *,
        config: AppConfig,
        repository: Repository,
        provider: ProviderClient,
        callback_url: str,
        output_path: str,
        poll_interval: float = 0.05,
        uncertain_request_grace: float = 2.2,
        max_504_replays: int = 3,
        request_arrival_guard_seconds: float = 0.1,
        reconciliation_concurrency: int = 10,
    ):
        self.config = config
        self.repository = repository
        self.provider = provider
        self.callback_url = callback_url
        self.output_path = output_path
        self.poll_interval = poll_interval
        self.uncertain_request_grace = uncertain_request_grace
        self.max_504_replays = max_504_replays
        if request_arrival_guard_seconds < 0:
            raise ValueError("request_arrival_guard_seconds must be non-negative")
        if reconciliation_concurrency < 1:
            raise ValueError("reconciliation_concurrency must be positive")
        self.request_arrival_guard_seconds = request_arrival_guard_seconds
        self.reconciliation_concurrency = reconciliation_concurrency
        self._stop = asyncio.Event()
        self._last_active_poll: dict[str, float] = {}

    async def _db_call(self, function, *args, **kwargs):
        """Run short synchronous psycopg operations outside the asyncio event loop."""
        return await asyncio.to_thread(function, *args, **kwargs)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("dispatcher started; waiting for provider simulated clock")
        while not self._stop.is_set():
            try:
                initial_clock = await self.provider.get_clock()
            except (httpx.HTTPError, ProviderError) as exc:
                logger.warning("provider clock unavailable: %s", exc)
                await asyncio.sleep(0.5)
                continue

            # Reconcile first so ended calls free tenant/phone capacity and any
            # newly-arrived callback is visible before we select the next batch.
            await self._reconcile(initial_clock.now)
            if await self._db_call(self.repository.all_leads_final):
                path = await self._db_call(self.repository.export_final_state, self.output_path)
                logger.info("all leads final; wrote %s", path)
                return

            # _reconcile may perform several provider lookups. Do not schedule from
            # the older clock snapshot: refresh it so callback/retry due times are
            # evaluated against current simulated time.
            try:
                scheduling_clock = await self.provider.get_clock()
            except (httpx.HTTPError, ProviderError) as exc:
                logger.warning("provider clock unavailable after reconciliation: %s", exc)
                await asyncio.sleep(0.25)
                continue

            candidates = await self._db_call(
                self.repository.get_dispatch_candidates, scheduling_clock.now
            )
            dispatched = await self._dispatch_priority_batches(
                candidates, scheduling_clock.now
            )
            if not dispatched:
                await asyncio.sleep(self.poll_interval)

    async def _dispatch_priority_batches(
        self, candidates: list[dict[str, Any]], scheduling_now: datetime
    ) -> bool:
        """Submit due callbacks, then retries, then fresh leads.

        Same-priority candidates are reserved up to the remaining tenant capacity
        and submitted concurrently. Lower-priority work is considered only after
        the higher-priority batch has been submitted and active capacity is
        re-read from PostgreSQL. The database remains the final concurrency guard.
        """
        if not candidates:
            return False

        groups: dict[int, list[dict[str, Any]]] = {}
        for candidate in candidates:
            priority = int(candidate_priority(candidate["state"]))
            groups.setdefault(priority, []).append(candidate)

        dispatched_any = False
        # If a higher-priority queue is waiting on a tenant whose capacity has
        # been filled, do not let lower-priority work for that same tenant take
        # newly-freed slots before the next scheduler pass re-evaluates callbacks.
        # Other tenants may still use their independent capacity.
        blocked_tenants: set[str] = set()
        for priority in sorted(groups):
            # Re-read durable active state between priority groups. This accounts
            # for synchronous 503/window deferrals and results that finish while
            # the previous batch is being submitted.
            active = await self._db_call(self.repository.active_attempts)
            active_by_tenant: dict[str, int] = {}
            for attempt in active:
                tenant_id = attempt["tenant_id"]
                active_by_tenant[tenant_id] = active_by_tenant.get(tenant_id, 0) + 1
            remaining = {
                tenant.tenant_id: max(
                    0, tenant.max_concurrent_calls - active_by_tenant.get(tenant.tenant_id, 0)
                )
                for tenant in self.config.tenants
            }

            reserved: list[dict[str, Any]] = []
            priority_blocked_tenants: set[str] = set()
            for candidate in groups[priority]:
                campaign_id = candidate["campaign_id"]
                tenant_id = self.repository.campaigns[campaign_id].tenant_id
                if tenant_id in blocked_tenants:
                    continue
                if remaining.get(tenant_id, 0) <= 0:
                    priority_blocked_tenants.add(tenant_id)
                    continue
                schedule = self.repository.schedules[campaign_id]
                if not is_allowed_start(scheduling_now, schedule):
                    continue
                attempt = await self._db_call(
                    self.repository.reserve_attempt, candidate, scheduling_now
                )
                if attempt is None:
                    continue
                remaining[tenant_id] -= 1
                reserved.append(attempt)

            blocked_tenants.update(priority_blocked_tenants)
            if reserved:
                dispatched_any = True
                results = await asyncio.gather(
                    *(
                        self._submit_reserved_attempt(attempt, scheduling_now)
                        for attempt in reserved
                    ),
                    return_exceptions=True,
                )
                errors = [result for result in results if isinstance(result, BaseException)]
                if errors:
                    # Let every sibling submission settle before surfacing a provider
                    # violation or an unexpected failure to the service supervisor.
                    raise errors[0]

        return dispatched_any

    async def _submit_reserved_attempt(self, attempt: dict[str, Any], now: datetime) -> None:
        payload = {
            "request_id": attempt["request_id"],
            "campaign_id": attempt["campaign_id"],
            "lead_id": attempt["lead_id"],
            "phone": attempt["phone"],
            "callback_url": self.callback_url,
        }
        for replay in range(self.max_504_replays + 1):
            # Capture monotonic time before the clock request. At 600x, even a
            # few hundred milliseconds of HTTP/event-loop delay is several
            # simulated minutes. Project the sample to a conservative estimate of
            # when the provider will evaluate the following POST.
            clock_request_started = time.monotonic()
            try:
                current_clock = await self.provider.get_clock()
            except (httpx.HTTPError, ProviderError) as exc:
                logger.warning(
                    "cannot verify calling window before %s: %s", attempt["request_id"], exc
                )
                # Preserve the durable requesting reservation. The scheduler has
                # not sent this POST, but keeping the same request_id lets the
                # normal reconciliation path safely confirm absence before replay.
                # This is deliberately conservative: no phone/tenant slot is
                # released while the authoritative provider clock is unavailable.
                return

            estimated_submit_at = estimate_submission_time(
                current_clock,
                clock_request_started,
                time.monotonic(),
                request_arrival_guard_seconds=self.request_arrival_guard_seconds,
            )
            schedule = self.repository.schedules[attempt["campaign_id"]]
            if not is_allowed_start(estimated_submit_at, schedule):
                await self._db_call(
                    self.repository.mark_retryable,
                    attempt["request_id"], estimated_submit_at,
                    "provider record absent; projected POST arrival is outside the permitted calling window",
                )
                logger.info(
                    "holding %s: projected provider submission time %s is outside the permitted window",
                    attempt["request_id"], estimated_submit_at.isoformat(),
                )
                return

            try:
                response = await self.provider.create_call(payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                await self._db_call(
                    self.repository.mark_unknown,
                    attempt["request_id"], estimated_submit_at,
                    f"transport outcome unknown ({type(exc).__name__})",
                )
                logger.warning(
                    "call outcome unknown for %s after %s; will reconcile",
                    attempt["request_id"], type(exc).__name__,
                )
                return

            if response.status_code in (200, 201):
                try:
                    body = response.json()
                except (ValueError, TypeError):
                    await self._db_call(
                        self.repository.mark_unknown,
                        attempt["request_id"], estimated_submit_at,
                        "provider returned malformed JSON after a successful call response",
                    )
                    logger.warning(
                        "malformed successful provider response for %s", attempt["request_id"]
                    )
                    return
                if not isinstance(body, dict) or body.get("request_id") != attempt["request_id"]:
                    await self._db_call(
                        self.repository.mark_unknown,
                        attempt["request_id"], estimated_submit_at,
                        "provider returned a mismatched call record after success",
                    )
                    logger.warning(
                        "mismatched successful provider response for %s", attempt["request_id"]
                    )
                    return
                await self._consume_provider_call(body, estimated_submit_at)
                return

            # Do not persist or log arbitrary provider response bodies: they may
            # echo customer PII such as phone numbers. Status codes are sufficient
            # for dispatcher decisions; detailed payloads remain with the provider.
            details = f"provider returned HTTP {response.status_code}"
            if response.status_code == 503:
                await self._db_call(
                    self.repository.mark_retryable,
                    attempt["request_id"], estimated_submit_at, details
                )
                logger.info(
                    "provider returned 503 for %s; no attempt counted", attempt["request_id"]
                )
                return

            if response.status_code == 504:
                await self._db_call(
                    self.repository.mark_unknown, attempt["request_id"], estimated_submit_at, details
                )
                # The 504 response means this request handler has finished. Looking up
                # the same key distinguishes the pre-accept timeout from a lost 201.
                try:
                    existing = await self.provider.get_call_by_request_id(attempt["request_id"])
                except (httpx.HTTPError, ProviderError):
                    return
                if existing is not None:
                    await self._consume_provider_call(existing, estimated_submit_at)
                    return
                if replay < self.max_504_replays:
                    logger.info(
                        "504 had no provider record for %s; rechecking the window before replay",
                        attempt["request_id"],
                    )
                    continue
                return

            if response.status_code in (409, 422, 429):
                raise ProviderViolation(
                    f"Provider recorded a business-rule violation for {attempt['request_id']}: "
                    f"HTTP {response.status_code} {details}"
                )

            if response.status_code >= 500:
                await self._db_call(
                    self.repository.mark_unknown,
                    attempt["request_id"], estimated_submit_at, details
                )
                logger.warning(
                    "unexpected provider error %s for %s",
                    response.status_code, attempt["request_id"],
                )
                return

            raise ProviderError(
                f"Unexpected provider response HTTP {response.status_code}: {details}",
                status_code=response.status_code,
                details=details,
            )

    async def _consume_provider_call(
        self, body: dict[str, Any], fallback_now: datetime | None = None
    ) -> None:
        if not isinstance(body, dict) or not isinstance(body.get("request_id"), str):
            raise ProviderError("Provider call record is missing a valid request_id")
        request_id = body["request_id"]
        result = body.get("result")
        if result is not None:
            if (
                not isinstance(result, dict)
                or result.get("request_id") != request_id
                or result.get("call_sid") != body.get("call_sid")
            ):
                observed_at = fallback_now or (await self.provider.get_clock()).now
                await self._db_call(
                    self.repository.mark_unknown, request_id, observed_at,
                    "provider returned an inconsistent ended-call record",
                )
                logger.warning("inconsistent ended-call record for %s; retaining reservation", request_id)
                return
            # This also handles a webhook that arrived before the original 201/504 response.
            await self._db_call(self.repository.apply_provider_result, result)
            self._last_active_poll.pop(request_id, None)
            return
        call_sid = body.get("call_sid")
        if not isinstance(call_sid, str) or not call_sid.strip():
            observed_at = fallback_now or (await self.provider.get_clock()).now
            await self._db_call(
                self.repository.mark_unknown, request_id, observed_at,
                "provider call record is missing a valid call_sid",
            )
            logger.warning("provider call record for %s has no call_sid; retaining reservation", request_id)
            return
        created_at = body.get("created_at")
        try:
            accepted_at = datetime.fromisoformat(created_at) if created_at else (await self.provider.get_clock()).now
            if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
                raise ValueError("provider created_at must include a timezone")
        except (TypeError, ValueError):
            observed_at = fallback_now or (await self.provider.get_clock()).now
            await self._db_call(
                self.repository.mark_unknown, request_id, observed_at,
                "provider call record has an invalid created_at timestamp",
            )
            logger.warning("provider call record for %s has an invalid created_at", request_id)
            return
        await self._db_call(self.repository.mark_accepted, request_id, call_sid, accepted_at)

    async def _reconcile(self, now: datetime) -> None:
        current_real = time.monotonic()
        attempts = await self._db_call(self.repository.active_attempts)
        semaphore = asyncio.Semaphore(self.reconciliation_concurrency)

        async def reconcile_one(attempt: dict[str, Any]) -> None:
            async with semaphore:
                request_id = attempt["request_id"]
                state = attempt["state"]
                last_poll = self._last_active_poll.get(request_id, 0.0)
                if state == "accepted" and current_real - last_poll < self.poll_interval:
                    return
                if state in ("requesting", "unknown"):
                    age = time.time() - float(attempt["updated_real_at"] or 0.0)
                    if age < self.uncertain_request_grace:
                        return
                self._last_active_poll[request_id] = current_real
                try:
                    if state == "accepted" and attempt.get("call_sid"):
                        body = await self.provider.get_call_by_sid(attempt["call_sid"])
                        if body is None:
                            body = await self.provider.get_call_by_request_id(request_id)
                    else:
                        body = await self.provider.get_call_by_request_id(request_id)
                except (httpx.HTTPError, ProviderError) as exc:
                    logger.warning(
                        "reconciliation lookup failed for %s: %s", request_id, exc
                    )
                    return

                if body is not None:
                    await self._consume_provider_call(body, now)
                    return

                # The provider has no record for the idempotency key. The original
                # request is older than the uncertainty window; retry with that SAME key.
                if state in ("requesting", "unknown"):
                    try:
                        refreshed = await self.provider.get_clock()
                    except (httpx.HTTPError, ProviderError) as exc:
                        logger.warning(
                            "provider clock unavailable during recovery of %s: %s",
                            request_id, exc,
                        )
                        return
                    await self._db_call(
                        self.repository.mark_unknown,
                        request_id,
                        refreshed.now,
                        "provider lookup returned 404; safe replay pending",
                    )
                    await self._submit_reserved_attempt(attempt, refreshed.now)

        await asyncio.gather(*(reconcile_one(attempt) for attempt in attempts))

