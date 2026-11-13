from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import time
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class ProviderClock:
    now: datetime
    speed: float
    sim_epoch: datetime | None = None
    real_epoch: float | None = None


def estimate_submission_time(
    clock: ProviderClock,
    request_started_monotonic: float,
    observed_monotonic: float,
    *,
    request_arrival_guard_seconds: float = 0.1,
) -> datetime:
    """Conservatively project a clock sample to the provider POST arrival time.

    The provider clock advances independently at ``speed`` simulated seconds per
    real second. Measuring from *before* the clock GET makes the elapsed duration
    an upper bound on the sample's age. A small real-time guard covers the local
    POST transit/event-loop handoff after the projection is calculated.
    """
    if clock.now.tzinfo is None or clock.now.utcoffset() is None:
        raise ValueError("Provider clock must be timezone-aware")
    if clock.speed <= 0:
        raise ValueError("Provider clock speed must be positive")
    if request_arrival_guard_seconds < 0:
        raise ValueError("Request arrival guard must be non-negative")
    if clock.sim_epoch is not None and clock.real_epoch is not None:
        if clock.sim_epoch.tzinfo is None or clock.sim_epoch.utcoffset() is None:
            raise ValueError("Provider clock epoch must be timezone-aware")
        if not math.isfinite(clock.real_epoch):
            raise ValueError("Provider real epoch must be finite")
        # The provider explicitly exposes sim_epoch/real_epoch and documents
        # sim_now = sim_epoch + (time.time() - real_epoch) * speed. Because the
        # provider is localhost, this uses the same host wall clock and projects
        # directly to the POST handoff moment rather than trusting an aged sample.
        elapsed_real = max(0.0, time.time() - clock.real_epoch)
        projected = clock.sim_epoch + timedelta(
            seconds=(elapsed_real + request_arrival_guard_seconds) * clock.speed
        )
        return max(clock.now, projected)

    # Fallback for test doubles or compatible providers which return only sim_now.
    elapsed_real = max(0.0, observed_monotonic - request_started_monotonic)
    projected_seconds = (elapsed_real + request_arrival_guard_seconds) * clock.speed
    return clock.now + timedelta(seconds=projected_seconds)


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class ProviderClient:
    @staticmethod
    def _decode_object(response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                f"Provider returned invalid JSON for {operation}",
                status_code=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise ProviderError(
                f"Provider returned a non-object response for {operation}",
                status_code=response.status_code,
            )
        return body

    def __init__(self, base_url: str, *, timeout_seconds: float = 8.0):
        timeout = httpx.Timeout(
            connect=2.0,
            read=timeout_seconds,
            write=2.0,
            pool=2.0,
        )
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def get_clock(self) -> ProviderClock:
        response = await self.client.get("/v1/clock")
        if response.status_code != 200:
            raise ProviderError("Could not read provider simulated clock", status_code=response.status_code)
        body = self._decode_object(response, "clock")
        try:
            now = datetime.fromisoformat(body["sim_now"])
            speed = float(body["speed"])
            sim_epoch = datetime.fromisoformat(body["sim_epoch"]) if body.get("sim_epoch") else None
            real_epoch = float(body["real_epoch"]) if body.get("real_epoch") is not None else None
            if (
                now.tzinfo is None
                or now.utcoffset() is None
                or not math.isfinite(speed)
                or speed <= 0
                or (sim_epoch is not None and (sim_epoch.tzinfo is None or sim_epoch.utcoffset() is None))
                or (real_epoch is not None and not math.isfinite(real_epoch))
                or ((sim_epoch is None) != (real_epoch is None))
            ):
                raise ValueError("clock values must be timezone-aware and speed/epochs must be valid")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Provider returned an invalid simulated clock") from exc
        return ProviderClock(now=now, speed=speed, sim_epoch=sim_epoch, real_epoch=real_epoch)

    async def create_call(self, payload: dict[str, str]) -> httpx.Response:
        return await self.client.post("/v1/calls", json=payload)

    async def get_call_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        response = await self.client.get("/v1/calls", params={"request_id": request_id})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProviderError(
                "Provider lookup by request_id failed",
                status_code=response.status_code,
                details=f"HTTP {response.status_code}",
            )
        body = self._decode_object(response, "call lookup by request_id")
        if body.get("request_id") != request_id:
            raise ProviderError("Provider request_id lookup returned a mismatched record")
        return body

    async def get_call_by_sid(self, call_sid: str) -> dict[str, Any] | None:
        response = await self.client.get(f"/v1/calls/{call_sid}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProviderError(
                "Provider lookup by call_sid failed",
                status_code=response.status_code,
                details=f"HTTP {response.status_code}",
            )
        body = self._decode_object(response, "call lookup by call_sid")
        if body.get("call_sid") != call_sid:
            raise ProviderError("Provider call_sid lookup returned a mismatched record")
        return body
