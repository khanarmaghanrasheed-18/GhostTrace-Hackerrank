"""Terminal entry point for GhostTrace.

Commands (run from the repository root):
  run           write output.csv for all evaluation requests
  validate      check an existing output.csv against the challenge contract
  inspect       one case's prediction and investigation detail
  graph         the case's evidence graph at audit_at
  timeline      the case's recorded events and observed states in time order
  whatif        hypothetical cleanup analysis (never changes data or output.csv)
  gemini-check  one live model call, only when GEMINI_API_KEY is configured
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

from ghosttrace import whatif
from ghosttrace.engine import AuditEngine
from ghosttrace.gemini import GeminiInterpreter
from ghosttrace.ingest import read_csv
from ghosttrace.validate import validate_file
from ghosttrace.views import case_graph, case_timeline, render_graph, render_timeline


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
    for name, text in (("graph", "focused evidence graph for one case at audit_at"),
                       ("timeline", "events and observed states for one case in time order")):
        view = sub.add_parser(name, help=text)
        view.add_argument("case_id")
        view.add_argument("--input", default="dataset/deletion_requests.csv")
        view.add_argument("--all", action="store_true", help="also list items after audit_at, marked as such")
        view.add_argument("--json", action="store_true")
        view.add_argument("--no-gemini", action="store_true")
    whatif = sub.add_parser("whatif", help="test proposed cleanup actions on a temporary copy of a case")
    whatif.add_argument("case_id")
    whatif.add_argument("--input", default="dataset/deletion_requests.csv")
    whatif.add_argument("--add", action="append", default=[], metavar="'ACTION RESOURCE'",
                        help="for example 'DISABLE_DATA_FLOW data_flow_3827'; repeatable")
    whatif.add_argument("--ask", help="natural-language question, translated by Gemini when configured")
    whatif.add_argument("--reset", action="store_true", help="discard the stored scenario first")
    whatif.add_argument("--reword", action="store_true", help="optional second Gemini call to reword the result")
    whatif.add_argument("--json", action="store_true")
    whatif.add_argument("--no-gemini", action="store_true")
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
    if command in ("inspect", "graph", "timeline", "whatif"):
        request = next((r for r in requests if r["case_id"] == args.case_id), None)
        if request is None:
            # Sample cases live in a separate file; look there before giving up.
            samples = read_csv(ROOT / "dataset/sample_deletion_requests.csv")
            request = next((r for r in samples if r["case_id"] == args.case_id), None)
        if request is None:
            parser.error(f"unknown case_id: {args.case_id}")
    if command == "graph":
        view = case_graph(engine, request, include_later=args.all)
        print(json.dumps(view, indent=2, ensure_ascii=False) if args.json else render_graph(view))
        return 0
    if command == "timeline":
        view = case_timeline(engine, request, include_later=args.all)
        print(json.dumps(view, indent=2, ensure_ascii=False) if args.json else render_timeline(view))
        return 0
    if command == "whatif":
        return run_whatif(engine, request, args)
    if command == "inspect":
        row, detail = engine.investigate(request)
        print(json.dumps({"prediction": {**row, "steps_json": json.loads(row["steps_json"])},
                          "investigation": detail, "extraction_errors": engine.evidence.extraction_errors,
                          "gemini_usage": engine.gemini.usage() if engine.gemini else None},
                         indent=2, ensure_ascii=False))
        return 0
    rows = []
    gaps = {"unknown_state_cases": 0, "cases_with_tied_workflow_events": 0,
            "cases_without_account_step": 0, "cases_with_no_steps": 0}
    for request in requests:
        row, detail = engine.investigate(request)
        rows.append(row)
        if row["deletion_status"] == "NOT_APPLICABLE":
            continue
        steps = json.loads(row["steps_json"])
        gaps["unknown_state_cases"] += bool(detail.get("uncertain"))
        gaps["cases_with_tied_workflow_events"] += bool(detail.get("unassigned_workflow_resources"))
        gaps["cases_without_account_step"] += not any(s["resource_id"].startswith("account_") for s in steps)
        gaps["cases_with_no_steps"] += not steps
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
                      "statuses": dict(Counter(r["deletion_status"] for r in rows)), "gaps": gaps,
                      "extraction_errors": engine.evidence.extraction_errors,
                      "gemini_usage": engine.gemini.usage() if engine.gemini else None}, indent=2))
    return 1 if errors else 0


def run_whatif(engine: AuditEngine, request: dict[str, str], args: argparse.Namespace) -> int:
    """Add proposals to the case's stored scenario and print the hypothetical result."""
    if args.reset:
        whatif.reset_scenario(ROOT, request["case_id"])
    actions = whatif.load_scenario(ROOT, request["case_id"])
    model = whatif.CaseModel(engine, request)
    messages = []
    for text in args.add:
        action, resource = whatif.parse_action(text)
        check = whatif.validate(model, action, resource)
        if check["status"] == "accepted":
            actions = whatif.add_actions(actions, [check])
        else:
            messages.append(check)
    interpretation = None
    if args.ask:
        interpretation = whatif.interpret(model, args.ask, engine.gemini)
        actions = whatif.add_actions(actions, interpretation["accepted"])
    if actions:
        whatif.save_scenario(ROOT, request["case_id"], actions)
    else:
        whatif.reset_scenario(ROOT, request["case_id"])
    result = whatif.assess(model, actions)
    explanation = whatif.explain(result)
    reworded = engine.gemini.reword(explanation) if args.reword and engine.gemini and engine.gemini.ready else None
    if args.json:
        print(json.dumps({"interpretation": interpretation, "messages": messages, "result": result,
                          "explanation": explanation, "model_wording": reworded,
                          "gemini_usage": engine.gemini.usage() if engine.gemini else None},
                         indent=2, ensure_ascii=False))
        return 0
    if interpretation:
        print(f"Question: {args.ask}")
        print(f"Interpreter: {interpretation['interpreter']}")
        for item in interpretation["accepted"]:
            print(f"  accepted: {item['action']} {item['resource_id']}")
        for item in interpretation["clarify"]:
            print(f"  clarification needed: {item['question']}")
        for item in interpretation["rejected"]:
            print(f"  rejected: {item.get('action')} {item.get('resource_id')} - {item.get('reason')}")
    for item in messages:
        detail = item.get("reason", "")
        if item["status"] == "clarify":
            detail += f"; options: {', '.join(item['candidates'])}"
        print(f"{item['status']}: {item['action']} {item.get('resource_id')} - {detail}")
    print(explanation)
    if args.reword:
        print("\nOptional model wording:" if reworded else "\nOptional model wording: not produced (Gemini unavailable)")
        if reworded:
            print(reworded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
