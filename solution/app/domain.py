from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import IntEnum, StrEnum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TERMINAL_DISPOSITIONS = frozenset(
    {"interested", "not_interested", "wrong_number", "dnd"}
)
RETRY_DELAYS_MINUTES = {
    "no_answer": 60,
    "busy": 60,
    "failed": 15,
}


class LeadState(StrEnum):
    PENDING = "pending"
    DIALING = "dialing"
    RETRY_WAIT = "retry_wait"
    CALLBACK_WAIT = "callback_wait"
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    DND_BLOCKED = "dnd_blocked"


class Priority(IntEnum):
    CALLBACK = 0
    RETRY = 1
    FRESH = 2


@dataclass(frozen=True, slots=True)
class CallingWindow:
    start: time
    end: time
    days: frozenset[str] | None = None

    @classmethod
    def from_config(cls, value: dict) -> "CallingWindow":
        start = time.fromisoformat(value["start"])
        end = time.fromisoformat(value["end"])
        days_value = value.get("days")
        days = frozenset(days_value) if days_value is not None else None
        if start >= end:
            raise ValueError("Calling windows must start before they end")
        if days is not None and not days.issubset(WEEKDAYS):
            raise ValueError("Calling window contains an invalid weekday")
        return cls(start=start, end=end, days=days)

    def allows_day(self, value: date) -> bool:
        return self.days is None or WEEKDAYS[value.weekday()] in self.days


@dataclass(frozen=True, slots=True)
class CampaignSchedule:
    campaign: CallingWindow
    regulatory: CallingWindow
    grace_seconds: int = 0


def as_ist(value: datetime) -> datetime:
    """Return an aware Asia/Kolkata datetime; reject naive timestamps."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(IST)


def is_allowed_start(at: datetime, schedule: CampaignSchedule) -> bool:
    """Match the provider's window check, including its clock-skew allowance.

    The provider tests the sampled instant and instants +/- grace_seconds
    independently against the campaign and regulatory windows. The actual
    configured windows remain start-inclusive/end-exclusive.
    """
    local = as_ist(at)
    grace = max(0, schedule.grace_seconds)
    probes = (
        local,
        local - timedelta(seconds=grace),
        local + timedelta(seconds=grace),
    ) if grace else (local,)

    def matches(window: CallingWindow) -> bool:
        for probe in probes:
            if not window.allows_day(probe.date()):
                continue
            current = probe.timetz().replace(tzinfo=None)
            if window.start <= current < window.end:
                return True
        return False

    return matches(schedule.campaign) and matches(schedule.regulatory)


def next_eligible_in_window(
    not_before: datetime,
    schedule: CampaignSchedule,
    *,
    search_days: int = 15,
) -> datetime:
    """Find the earliest instant >= not_before inside both allowed windows.

    Campaign windows are day-scoped and non-overnight in the supplied fixture.
    The explicit bound prevents an accidental infinite search on bad config.
    """
    start_at = as_ist(not_before)
    campaign = schedule.campaign
    regulatory = schedule.regulatory
    for offset in range(search_days + 1):
        day = start_at.date() + timedelta(days=offset)
        if not campaign.allows_day(day) or not regulatory.allows_day(day):
            continue
        opening = max(campaign.start, regulatory.start)
        closing = min(campaign.end, regulatory.end)
        if opening >= closing:
            continue
        window_open = datetime.combine(day, opening, tzinfo=IST)
        window_close = datetime.combine(day, closing, tzinfo=IST)
        candidate = max(start_at, window_open)
        if candidate < window_close:
            return candidate
    raise ValueError(f"No eligible calling window found within {search_days} days")


def retry_not_before(
    last_status: str,
    ended_at: datetime,
    retry_policy: dict[str, int] | None = None,
) -> datetime:
    """Business retry delay is measured from provider ended_at, in simulated time."""
    policy = retry_policy if retry_policy is not None else RETRY_DELAYS_MINUTES
    try:
        delay = policy[last_status]
    except KeyError as exc:
        raise ValueError(f"No retry policy for status {last_status!r}") from exc
    if delay < 0:
        raise ValueError("Retry delay cannot be negative")
    return as_ist(ended_at) + timedelta(minutes=delay)


def candidate_priority(state: str) -> Priority:
    if state == LeadState.CALLBACK_WAIT:
        return Priority.CALLBACK
    if state == LeadState.RETRY_WAIT:
        return Priority.RETRY
    if state == LeadState.PENDING:
        return Priority.FRESH
    raise ValueError(f"Lead state {state!r} is not dispatchable")


def sort_dispatch_candidates(candidates: list[dict]) -> list[dict]:
    """Order by callback/retry/fresh priority, then due time and stable IDs."""
    def key(item: dict) -> tuple[int, str, str, str]:
        return (
            int(candidate_priority(item["state"])),
            item["eligible_at"].isoformat(),
            item["campaign_id"],
            item["lead_id"],
        )

    return sorted(candidates, key=key)


def derive_final_state(
    *,
    terminal_disposition: str | None,
    accepted_attempts: int,
    max_attempts: int,
    phone_is_dnd: bool,
    has_active_call: bool,
) -> str | None:
    """Mirror provider precedence: completed > exhausted > dnd_blocked."""
    if terminal_disposition in TERMINAL_DISPOSITIONS:
        return LeadState.COMPLETED
    if has_active_call:
        return None
    if accepted_attempts >= max_attempts:
        return LeadState.EXHAUSTED
    if phone_is_dnd:
        return LeadState.DND_BLOCKED
    return None
