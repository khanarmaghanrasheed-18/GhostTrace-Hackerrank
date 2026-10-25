"""Strict structural validation of challenge output."""

from __future__ import annotations

import csv
import json
from pathlib import Path

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
OPERATIONS = {
    "FINDING": [
        ("REPORT_REMAINING_RECORD", "PRESENT"), ("REPORT_EXPOSED_PII", "PRESENT"),
        ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"), ("REPORT_RESTORE_RISK", "ACTIVE")],
    "REMEDIATION": [
        ("DISABLE_DATA_FLOW", "DISABLED"), ("BLOCK_RESTORE", "BLOCKED"),
        ("DELETE_RECORD", "ABSENT"), ("REDACT_RECORD", "REDACTED"),
        ("ANONYMIZE_RECORD", "ANONYMIZED")],
    "VERIFICATION": [
        ("VERIFY_RECORD_ABSENT", "ABSENT"), ("VERIFY_RECORD_PRESENT", "PRESENT"),
        ("VERIFY_RECORD_REDACTED", "REDACTED"), ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
        ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"), ("VERIFY_RESTORE_BLOCKED", "BLOCKED")],
}
PHASE_ORDER = {name: n for n, name in enumerate(OPERATIONS)}


def validate_rows(rows: list[dict[str, str]], request_ids: list[str], valid_evidence: set[str],
                  evidence_times: dict[str, str] | None = None,
                  cutoffs: dict[str, str] | None = None) -> list[str]:
    errors: list[str] = []
    actual_ids = [r.get("case_id", "") for r in rows]
    if actual_ids != sorted(request_ids):
        errors.append("case IDs must exactly match requests in sorted order")
    for row in rows:
        case = row.get("case_id", "")
        status = row.get("deletion_status", "")
        if status not in {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}:
            errors.append(f"{case}: invalid status")
        try:
            steps = json.loads(row.get("steps_json", ""))
        except json.JSONDecodeError:
            errors.append(f"{case}: steps_json is invalid JSON")
            continue
        if not isinstance(steps, list):
            errors.append(f"{case}: steps_json must be an array")
            continue
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{case}: NOT_APPLICABLE needs []")
        prev = (-1, -1, "", "")
        for n, step in enumerate(steps, 1):
            if not isinstance(step, dict) or set(step) != set(FIELDS):
                errors.append(f"{case}: step {n} fields invalid")
                continue
            phase = step["phase"]
            if phase not in OPERATIONS or (step["operation"], step["state"]) not in OPERATIONS[phase]:
                errors.append(f"{case}: step {n} operation/state invalid")
                continue
            if step["step_no"] != n:
                errors.append(f"{case}: step {n} number invalid")
            key = (PHASE_ORDER[phase], OPERATIONS[phase].index((step["operation"], step["state"])),
                   step["system_id"], step["resource_id"])
            if key < prev:
                errors.append(f"{case}: step {n} ordering invalid")
            prev = key
            evidence = step["evidence_id"]
            if phase == "FINDING" or (status == "COMPLETE" and phase == "VERIFICATION"):
                if evidence not in valid_evidence:
                    errors.append(f"{case}: step {n} lacks valid evidence")
                elif evidence_times and cutoffs and evidence_times.get(evidence, "") > cutoffs.get(case, ""):
                    errors.append(f"{case}: step {n} cites evidence after audit_at")
            elif evidence:
                errors.append(f"{case}: step {n} should have empty evidence_id")
            if status == "COMPLETE" and phase != "VERIFICATION":
                errors.append(f"{case}: COMPLETE may only verify")
    return errors


def validate_file(path: Path, request_ids: list[str], valid_evidence: set[str],
                  evidence_times: dict[str, str] | None = None,
                  cutoffs: dict[str, str] | None = None) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["case_id", "deletion_status", "steps_json"]:
            return ["CSV header must be case_id,deletion_status,steps_json"]
        return validate_rows(list(reader), request_ids, valid_evidence, evidence_times, cutoffs)
