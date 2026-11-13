import json

import pytest

from scripts.verify_provider_output import VerificationError, compare_results


def _fixtures(path):
    path.write_text(
        json.dumps({"tenants": [{"tenant_id": "t1", "max_concurrent_calls": 2}]}),
        encoding="utf-8",
    )
    return path


def _csv(path, rows):
    path.write_text(
        "campaign_id,lead_id,final_state,attempts,last_status,last_disposition\n" + rows,
        encoding="utf-8",
    )
    return path


def _report(**overrides):
    base = {
        "violation_count": 0,
        "calls_active": 0,
        "calls_total": 1,
        "max_concurrency_seen": {"t1": 2},
    }
    return {**base, **overrides}


def test_exact_provider_csv_match_passes(tmp_path):
    csv_path = _csv(tmp_path / "final_state.csv", "c1,l1,completed,1,completed,interested\nc1,l2,exhausted,3,no_answer,\n")
    leads = [
        {"campaign_id": "c1", "lead_id": "l1", "expected_state": "completed", "attempts": 1,
         "last_status": "completed", "last_disposition": "interested"},
        {"campaign_id": "c1", "lead_id": "l2", "expected_state": "exhausted", "attempts": 3,
         "last_status": "no_answer", "last_disposition": ""},
    ]
    result = compare_results(
        csv_path, leads, _report(calls_total=4), _fixtures(tmp_path / "campaigns.json")
    )
    assert result["exact_match"] is True
    assert result["mismatch_count"] == 0
    assert result["provider_violation_count"] == 0
    assert result["provider_calls_total"] == result["csv_accepted_attempts"] == 4


def test_verifier_surfaces_callback_lateness_metrics(tmp_path):
    csv_path = _csv(tmp_path / "final_state.csv", "c1,l1,completed,1,completed,interested\n")
    lateness = {"count": 1, "p50": 1.2, "p90": 1.2, "max": 1.2}
    result = compare_results(
        csv_path,
        [{"campaign_id": "c1", "lead_id": "l1", "expected_state": "completed", "attempts": 1,
          "last_status": "completed", "last_disposition": "interested"}],
        _report(callback_lateness_minutes=lateness),
        _fixtures(tmp_path / "campaigns.json"),
    )
    assert result["callback_lateness_minutes"] == lateness


def test_csv_contract_and_row_sort_are_enforced(tmp_path):
    csv_path = _csv(tmp_path / "final_state.csv", "c1,l2,exhausted,3,no_answer,\nc1,l1,completed,1,completed,interested\n")
    with pytest.raises(VerificationError, match="not sorted"):
        compare_results(
            csv_path,
            [
                {"campaign_id": "c1", "lead_id": "l1", "expected_state": "completed", "attempts": 1},
                {"campaign_id": "c1", "lead_id": "l2", "expected_state": "exhausted", "attempts": 3},
            ],
            _report(),
            _fixtures(tmp_path / "campaigns.json"),
        )


def test_nonzero_provider_violations_fail(tmp_path):
    csv_path = _csv(tmp_path / "final_state.csv", "c1,l1,completed,1,completed,interested\n")
    with pytest.raises(VerificationError, match="violation"):
        compare_results(
            csv_path,
            [{"campaign_id": "c1", "lead_id": "l1", "expected_state": "completed", "attempts": 1,
              "last_status": "completed", "last_disposition": "interested"}],
            _report(violation_count=1),
            _fixtures(tmp_path / "campaigns.json"),
        )


def test_tenant_concurrency_over_limit_fails(tmp_path):
    csv_path = _csv(tmp_path / "final_state.csv", "c1,l1,completed,1,completed,interested\n")
    with pytest.raises(VerificationError, match="concurrency"):
        compare_results(
            csv_path,
            [{"campaign_id": "c1", "lead_id": "l1", "expected_state": "completed", "attempts": 1,
              "last_status": "completed", "last_disposition": "interested"}],
            _report(max_concurrency_seen={"t1": 3}),
            _fixtures(tmp_path / "campaigns.json"),
        )


def test_provider_call_total_must_match_csv_accepted_attempts(tmp_path):
    csv_path = _csv(tmp_path / "final_state.csv", "c1,l1,completed,1,completed,interested\n")
    with pytest.raises(VerificationError, match="Accepted call total"):
        compare_results(
            csv_path,
            [{"campaign_id": "c1", "lead_id": "l1", "expected_state": "completed", "attempts": 1,
              "last_status": "completed", "last_disposition": "interested"}],
            _report(calls_total=2),
            _fixtures(tmp_path / "campaigns.json"),
        )



def test_incomplete_provider_lead_is_never_accepted_as_final(tmp_path):
    csv_path = _csv(
        tmp_path / "final_state.csv",
        "c1,l1,incomplete,0,,\n",
    )
    with pytest.raises(VerificationError, match="not in a final state"):
        compare_results(
            csv_path,
            [{"campaign_id": "c1", "lead_id": "l1", "expected_state": "incomplete",
              "attempts": 0, "last_status": "", "last_disposition": ""}],
            _report(calls_total=0),
            _fixtures(tmp_path / "campaigns.json"),
        )
