from datetime import datetime, timedelta

import pytest

from app.domain import (
    CampaignSchedule,
    CallingWindow,
    LeadState,
    Priority,
    candidate_priority,
    derive_final_state,
    is_allowed_start,
    next_eligible_in_window,
    retry_not_before,
    sort_dispatch_candidates,
)


def schedule() -> CampaignSchedule:
    return CampaignSchedule(
        campaign=CallingWindow.from_config(
            {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat"]}
        ),
        regulatory=CallingWindow.from_config({"start": "09:00", "end": "21:00"}),
    )


def test_campaign_window_is_start_inclusive_and_end_exclusive():
    current = schedule()
    assert is_allowed_start(datetime.fromisoformat("2026-11-12T10:00:00+05:30"), current)
    assert is_allowed_start(datetime.fromisoformat("2026-11-12T18:59:59+05:30"), current)
    assert not is_allowed_start(datetime.fromisoformat("2026-11-12T19:00:00+05:30"), current)


def test_regulatory_window_is_intersected_with_campaign_window():
    current = CampaignSchedule(
        campaign=CallingWindow.from_config({"start": "08:00", "end": "22:00", "days": ["thu"]}),
        regulatory=CallingWindow.from_config({"start": "09:00", "end": "21:00"}),
    )
    assert not is_allowed_start(datetime.fromisoformat("2026-11-12T08:59:59+05:30"), current)
    assert is_allowed_start(datetime.fromisoformat("2026-11-12T09:00:00+05:30"), current)
    assert not is_allowed_start(datetime.fromisoformat("2026-11-12T21:00:00+05:30"), current)


def test_next_open_moves_after_close_to_next_allowed_day():
    result = next_eligible_in_window(datetime.fromisoformat("2026-11-12T19:01:00+05:30"), schedule())
    assert result.isoformat() == "2026-11-13T10:00:00+05:30"


def test_next_open_skips_sunday_for_weekday_only_campaign():
    result = next_eligible_in_window(datetime.fromisoformat("2026-11-14T19:01:00+05:30"), schedule())
    assert result.isoformat() == "2026-11-16T10:00:00+05:30"


@pytest.mark.parametrize(
    ("status", "minutes"),
    [("no_answer", 60), ("busy", 60), ("failed", 15)],
)
def test_retry_delay_is_measured_from_call_end(status, minutes):
    ended = datetime.fromisoformat("2026-11-12T17:30:00+05:30")
    assert retry_not_before(status, ended) == ended + timedelta(minutes=minutes)


def test_candidate_order_is_callback_then_retry_then_fresh():
    candidates = [
        {"state": LeadState.PENDING, "eligible_at": datetime.fromisoformat("2026-11-12T10:00:00+05:30"), "campaign_id": "c", "lead_id": "a"},
        {"state": LeadState.RETRY_WAIT, "eligible_at": datetime.fromisoformat("2026-11-12T11:00:00+05:30"), "campaign_id": "c", "lead_id": "b"},
        {"state": LeadState.CALLBACK_WAIT, "eligible_at": datetime.fromisoformat("2026-11-12T12:00:00+05:30"), "campaign_id": "c", "lead_id": "c"},
    ]
    ordered = sort_dispatch_candidates(candidates)
    assert [candidate_priority(item["state"]) for item in ordered] == [
        Priority.CALLBACK, Priority.RETRY, Priority.FRESH
    ]


def test_final_state_precedence_matches_provider_contract():
    assert derive_final_state(
        terminal_disposition="dnd", accepted_attempts=3, max_attempts=3,
        phone_is_dnd=True, has_active_call=False,
    ) == LeadState.COMPLETED
    assert derive_final_state(
        terminal_disposition=None, accepted_attempts=3, max_attempts=3,
        phone_is_dnd=True, has_active_call=False,
    ) == LeadState.EXHAUSTED
    assert derive_final_state(
        terminal_disposition=None, accepted_attempts=1, max_attempts=3,
        phone_is_dnd=True, has_active_call=False,
    ) == LeadState.DND_BLOCKED
    assert derive_final_state(
        terminal_disposition=None, accepted_attempts=3, max_attempts=3,
        phone_is_dnd=False, has_active_call=True,
    ) is None


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        is_allowed_start(datetime(2026, 11, 12, 10, 0), schedule())


def test_provider_clock_skew_grace_matches_campaign_and_regulatory_windows():
    current = CampaignSchedule(
        campaign=CallingWindow.from_config(
            {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat"]}
        ),
        regulatory=CallingWindow.from_config({"start": "09:00", "end": "21:00"}),
        grace_seconds=120,
    )
    # The supplied provider tolerates a +/-120 simulated-second clock skew.
    assert is_allowed_start(datetime.fromisoformat("2026-11-12T09:58:30+05:30"), current)
    assert is_allowed_start(datetime.fromisoformat("2026-11-12T19:01:30+05:30"), current)
    assert not is_allowed_start(datetime.fromisoformat("2026-11-12T09:57:59+05:30"), current)
    assert not is_allowed_start(datetime.fromisoformat("2026-11-12T19:02:01+05:30"), current)


def test_provider_clock_skew_grace_can_cross_midnight_for_weekday_checks():
    current = CampaignSchedule(
        campaign=CallingWindow.from_config({"start": "00:00", "end": "23:59", "days": ["thu"]}),
        regulatory=CallingWindow.from_config({"start": "00:00", "end": "23:59"}),
        grace_seconds=120,
    )
    # At Thursday 00:00:30, the -120s probe falls on Wednesday, but the exact
    # instant and +120s probe are Thursday and therefore within the allowed day.
    assert is_allowed_start(datetime.fromisoformat("2026-11-12T00:00:30+05:30"), current)
