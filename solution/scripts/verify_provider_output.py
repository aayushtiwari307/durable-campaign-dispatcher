from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

import httpx

EXPECTED_COLUMNS = [
    "campaign_id",
    "lead_id",
    "final_state",
    "attempts",
    "last_status",
    "last_disposition",
]
FINAL_STATES = {"completed", "exhausted", "dnd_blocked"}


class VerificationError(RuntimeError):
    pass


def compare_results(
    csv_path: str | Path,
    admin_leads: list[dict[str, Any]],
    admin_report: dict[str, Any],
    fixtures_path: str | Path,
) -> dict[str, Any]:
    """Compare final_state.csv with provider truth and verify report invariants."""
    path = Path(csv_path)
    if not path.is_file():
        raise VerificationError(f"Final CSV does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise VerificationError(
                f"CSV columns differ from contract: expected {EXPECTED_COLUMNS}, got {reader.fieldnames}"
            )
        actual_rows = list(reader)

    actual_by_key: dict[tuple[str, str], dict[str, str]] = {}
    actual_order: list[tuple[str, str]] = []
    for row in actual_rows:
        key = (row["campaign_id"], row["lead_id"])
        if key in actual_by_key:
            raise VerificationError(f"Duplicate lead row in final CSV: {key}")
        actual_by_key[key] = row
        actual_order.append(key)

    expected_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in admin_leads:
        key = (str(row["campaign_id"]), str(row["lead_id"]))
        if key in expected_by_key:
            raise VerificationError(f"Duplicate lead row in provider /admin/leads: {key}")
        expected_by_key[key] = row

    if set(actual_by_key) != set(expected_by_key):
        missing = sorted(set(expected_by_key) - set(actual_by_key))
        extra = sorted(set(actual_by_key) - set(expected_by_key))
        raise VerificationError(f"Lead identity mismatch; missing={missing[:10]}, extra={extra[:10]}")

    expected_order = sorted(expected_by_key)
    if actual_order != expected_order:
        raise VerificationError("Final CSV rows are not sorted by campaign_id, lead_id")

    mismatches: list[dict[str, Any]] = []
    for key in expected_order:
        expected = expected_by_key[key]
        actual = actual_by_key[key]
        expected_state = str(expected["expected_state"])
        actual_state = str(actual["final_state"])
        if expected_state not in FINAL_STATES:
            raise VerificationError(
                f"Provider lead {key} is not in a final state: {expected_state!r}"
            )
        if actual_state not in FINAL_STATES:
            raise VerificationError(
                f"CSV lead {key} has an invalid final_state: {actual_state!r}"
            )
        expected_values = {
            "final_state": expected_state,
            "attempts": str(expected["attempts"]),
            "last_status": str(expected.get("last_status") or ""),
            "last_disposition": str(expected.get("last_disposition") or ""),
        }
        differences = {
            field: {"expected": expected_value, "actual": actual[field]}
            for field, expected_value in expected_values.items()
            if actual[field] != expected_value
        }
        if differences:
            mismatches.append({"campaign_id": key[0], "lead_id": key[1], "differences": differences})

    with Path(fixtures_path).open(encoding="utf-8") as handle:
        fixtures = json.load(handle)
    tenant_limits = {
        str(item["tenant_id"]): int(item["max_concurrent_calls"])
        for item in fixtures["tenants"]
    }
    report_violations = int(admin_report.get("violation_count", -1))
    if report_violations != 0:
        raise VerificationError(f"Provider reports {report_violations} rule violation(s)")

    calls_active = int(admin_report.get("calls_active", -1))
    if calls_active != 0:
        raise VerificationError(f"Provider still reports {calls_active} active call(s)")

    provider_calls_total = int(admin_report.get("calls_total", -1))
    csv_accepted_attempts = sum(int(row["attempts"]) for row in actual_rows)
    if provider_calls_total != csv_accepted_attempts:
        raise VerificationError(
            "Accepted call total mismatch: "
            f"provider={provider_calls_total}, CSV attempts={csv_accepted_attempts}"
        )

    concurrency = admin_report.get("max_concurrency_seen") or {}
    concurrency_errors = {
        tenant_id: {"limit": limit, "observed": int(concurrency.get(tenant_id, 0))}
        for tenant_id, limit in tenant_limits.items()
        if int(concurrency.get(tenant_id, 0)) > limit
    }
    if concurrency_errors:
        raise VerificationError(f"Provider concurrency maxima exceed tenant limits: {concurrency_errors}")

    return {
        "csv_rows": len(actual_rows),
        "provider_leads": len(admin_leads),
        "exact_match": not mismatches,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "provider_violation_count": report_violations,
        "provider_active_calls": calls_active,
        "provider_calls_total": provider_calls_total,
        "csv_accepted_attempts": csv_accepted_attempts,
        "max_concurrency_seen": concurrency,
        "tenant_limits": tenant_limits,
        "callback_lateness_minutes": admin_report.get("callback_lateness_minutes", {}),
    }


async def fetch_and_compare(
    provider_url: str,
    csv_path: str | Path,
    fixtures_path: str | Path,
) -> dict[str, Any]:
    timeout = httpx.Timeout(10.0, connect=3.0)
    async with httpx.AsyncClient(base_url=provider_url.rstrip("/"), timeout=timeout) as client:
        try:
            leads_response, report_response = await asyncio.gather(
                client.get("/admin/leads"),
                client.get("/admin/report"),
            )
        except httpx.HTTPError as exc:
            raise VerificationError(f"Could not reach mock provider: {exc}") from exc
    if leads_response.status_code != 200 or report_response.status_code != 200:
        raise VerificationError(
            "Provider admin endpoint failed: "
            f"/admin/leads={leads_response.status_code}, /admin/report={report_response.status_code}"
        )
    try:
        admin_leads = leads_response.json()
        admin_report = report_response.json()
    except ValueError as exc:
        raise VerificationError("Provider admin endpoint returned invalid JSON") from exc
    if not isinstance(admin_leads, list) or not isinstance(admin_report, dict):
        raise VerificationError("Provider admin endpoints returned unexpected JSON shapes")
    return compare_results(csv_path, admin_leads, admin_report, fixtures_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare final_state.csv with mock provider admin endpoints"
    )
    parser.add_argument("--provider", default="http://127.0.0.1:8099")
    parser.add_argument("--csv", default="final_state.csv")
    parser.add_argument("--fixtures", default="assignment-b-workflow/fixtures/campaigns.json")
    args = parser.parse_args()
    try:
        result = asyncio.run(fetch_and_compare(args.provider, args.csv, args.fixtures))
    except (OSError, ValueError, KeyError, TypeError, VerificationError) as exc:
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["exact_match"]:
        print("VERIFICATION FAILED: CSV does not exactly match provider output", file=sys.stderr)
        return 1
    print("VERIFICATION PASSED: exact CSV match, zero provider violations, no active calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
