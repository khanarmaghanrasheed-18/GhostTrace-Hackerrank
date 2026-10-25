"""Terminal entry point for the GhostTrace core audit engine."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

from ghosttrace.engine import AuditEngine
from ghosttrace.gemini import GeminiInterpreter
from ghosttrace.ingest import read_csv
from ghosttrace.validate import validate_file


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evidence-backed deletion audit")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="write predictions for all evaluation requests")
    run.add_argument("--input", default="dataset/deletion_requests.csv")
    run.add_argument("--output", default="output.csv")
    run.add_argument("--no-gemini", action="store_true")
    inspect = sub.add_parser("inspect", help="show one case and supporting observations")
    inspect.add_argument("case_id")
    inspect.add_argument("--input", default="dataset/deletion_requests.csv")
    inspect.add_argument("--no-gemini", action="store_true")
    check = sub.add_parser("validate", help="validate an existing output CSV")
    check.add_argument("--input", default="dataset/deletion_requests.csv")
    check.add_argument("--output", default="output.csv")
    sub.add_parser("gemini-check", help="make one live model request only when a key is configured")
    args = parser.parse_args(sys.argv[1:] or ["run"])
    command = args.command
    if command == "gemini-check":
        model = GeminiInterpreter(ROOT)
        if not model.ready:
            print(json.dumps({"ready": False, "reason": "GEMINI_API_KEY is not configured", "usage": model.usage()}, indent=2))
            return 2
        answer = model.deletion_intent("Please delete my example account and its personal data.")
        print(json.dumps({"ready": answer is True, "validated_answer": answer, "usage": model.usage()}, indent=2))
        return 0 if answer is True else 2
    request_file = (ROOT / args.input).resolve()
    if not request_file.is_relative_to(ROOT):
        parser.error("input must be inside the repository")
    requests = read_csv(request_file)
    evidence_times = {r["file_id"]: r["captured_at"] for r in read_csv(ROOT / "dataset/evidence_files.csv")}
    evidence_times.update({r["activity_id"]: r["event_time"] for r in read_csv(ROOT / "dataset/system_activity.csv")})
    evidence_times.update({r["audit_id"]: r["event_time"] for r in read_csv(ROOT / "dataset/user_audit_log.csv")})
    valid_evidence = set(evidence_times)
    cutoffs = {r["case_id"]: r["audit_at"] for r in requests}
    if command == "validate":
        errors = validate_file(ROOT / args.output, [r["case_id"] for r in requests], valid_evidence,
                               evidence_times, cutoffs)
        print(json.dumps({"valid": not errors, "errors": errors[:30], "error_count": len(errors)}, indent=2))
        return 1 if errors else 0
    engine = AuditEngine(ROOT, use_gemini=not args.no_gemini)
    if command == "inspect":
        request = next((r for r in requests if r["case_id"] == args.case_id), None)
        if request is None:
            parser.error(f"unknown case_id: {args.case_id}")
        row, detail = engine.investigate(request)
        print(json.dumps({"prediction": {**row, "steps_json": json.loads(row["steps_json"])},
                          "investigation": detail, "extraction_errors": engine.evidence.extraction_errors,
                          "gemini_usage": engine.gemini.usage() if engine.gemini else None},
                         indent=2, ensure_ascii=False))
        return 0
    rows = []
    uncertain = 0
    for request in requests:
        row, detail = engine.investigate(request)
        rows.append(row)
        if row["deletion_status"] != "NOT_APPLICABLE":
            uncertain += bool(detail.get("uncertain") or detail.get("ambiguous_identity") or not detail.get("resources"))
    rows.sort(key=lambda r: r["case_id"])
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT):
        parser.error("output must be inside the repository")
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "deletion_status", "steps_json"],
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    errors = validate_file(output, [r["case_id"] for r in requests], valid_evidence,
                           evidence_times, cutoffs)
    print(json.dumps({"output": str(output.relative_to(ROOT)), "rows": len(rows),
                      "valid": not errors, "errors": errors[:30], "error_count": len(errors),
                      "uncertain_cases": uncertain, "extraction_errors": engine.evidence.extraction_errors,
                      "gemini_usage": engine.gemini.usage() if engine.gemini else None}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
