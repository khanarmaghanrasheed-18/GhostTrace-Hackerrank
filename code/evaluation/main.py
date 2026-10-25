"""Public-sample evaluation; expected answers never enter prediction code.

Run from the repository root:
    python3 code/evaluation/main.py                      # score the current engine
    python3 code/evaluation/main.py --predictions F.csv  # score an existing CSV
    python3 code/evaluation/main.py --details            # list every differing step
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from ghosttrace.engine import AuditEngine  # noqa: E402
from ghosttrace.ingest import read_csv  # noqa: E402

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def step_key(step: dict) -> tuple:
    """A step without its number, so one missing step does not hide later matches."""
    return tuple(step[f] for f in FIELDS[1:])


def common_order_kept(predicted: list[tuple], expected: list[tuple]) -> bool:
    """True when the steps both lists contain appear in the same relative order."""
    shared = [k for k in predicted if k in expected]
    return shared == [k for k in expected if k in predicted]


def score(rows: dict[str, dict[str, str]], expected: dict[str, dict[str, str]]) -> dict:
    totals = Counter()
    per_case = []
    for case, gold in sorted(expected.items()):
        actual = rows.get(case, {"deletion_status": "", "steps_json": "[]"})
        pred = json.loads(actual["steps_json"])
        want = json.loads(gold["steps_json"])
        p_keys = [step_key(s) for s in pred]
        g_keys = [step_key(s) for s in want]
        matched = sum((Counter(p_keys) & Counter(g_keys)).values())
        # Field accuracy: align steps on (phase, resource_id) and compare all seven fields.
        by_slot = {(s["phase"], s["resource_id"]): s for s in pred}
        fields_equal = 0
        for g in want:
            p = by_slot.get((g["phase"], g["resource_id"]))
            if p:
                fields_equal += sum(p.get(f) == g.get(f) for f in FIELDS)
        totals["cases"] += 1
        totals["status_correct"] += actual["deletion_status"] == gold["deletion_status"]
        totals["exact_rows"] += actual["deletion_status"] == gold["deletion_status"] and pred == want
        totals["predicted_steps"] += len(pred)
        totals["expected_steps"] += len(want)
        totals["matched_steps"] += matched
        totals["fields_equal"] += fields_equal
        totals["expected_fields"] += len(want) * len(FIELDS)
        totals["order_kept"] += common_order_kept(p_keys, g_keys)
        totals["sequence_exact"] += p_keys == g_keys
        per_case.append({"case_id": case, "status": actual["deletion_status"],
                         "expected_status": gold["deletion_status"],
                         "missing": [k for k in g_keys if k not in p_keys],
                         "extra": [k for k in p_keys if k not in g_keys]})
    t = totals
    summary = {
        "cases": t["cases"],
        "status_accuracy": f"{t['status_correct']}/{t['cases']}",
        "exact_row_accuracy": f"{t['exact_rows']}/{t['cases']}",
        "step_precision": round(t["matched_steps"] / max(t["predicted_steps"], 1), 3),
        "step_recall": round(t["matched_steps"] / max(t["expected_steps"], 1), 3),
        "matched_steps": f"{t['matched_steps']} (predicted {t['predicted_steps']}, expected {t['expected_steps']})",
        "field_accuracy": round(t["fields_equal"] / max(t["expected_fields"], 1), 3),
        "sequence_exact": f"{t['sequence_exact']}/{t['cases']}",
        "relative_order_kept": f"{t['order_kept']}/{t['cases']}",
    }
    return {"summary": summary, "cases": per_case}


def explain_missing(engine: AuditEngine, requests: dict[str, dict[str, str]], result: dict) -> Counter:
    """Classify each missing step by the part of the pipeline that lost it."""
    mentioned = set(engine.by_resource) | {e["resource_id"] for e in engine.activity}
    causes = Counter()
    for case in result["cases"]:
        if not case["missing"]:
            continue
        _, detail = engine.investigate(requests[case["case_id"]])
        linked = {r["resource_id"]: r for r in detail.get("resources", [])}
        open_claims = set(detail.get("unassigned_workflow_resources", []))
        for phase, system, resource, operation, state, evidence in case["missing"]:
            if resource in linked:
                cause = "linked; different evidence or operation"
            elif resource in open_claims:
                cause = "workflow event tied between requests"
            elif resource not in mentioned:
                cause = "resource absent from all participant evidence"
            else:
                cause = "resource not linked to the person"
            causes[cause] += 1
            case.setdefault("causes", []).append([resource, operation, cause])
    return causes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", help="score an existing CSV instead of running the engine")
    parser.add_argument("--details", action="store_true", help="print every differing case")
    args = parser.parse_args()
    requests = {r["case_id"]: r for r in read_csv(ROOT / "dataset/sample_deletion_requests.csv")}
    expected = {r["case_id"]: r for r in read_csv(ROOT / "dataset/sample_output.csv")}
    engine = None
    if args.predictions:
        rows = {r["case_id"]: r for r in read_csv(Path(args.predictions))}
    else:
        engine = AuditEngine(ROOT, use_gemini=False)
        rows = {case: engine.investigate(request)[0] for case, request in requests.items()}
    result = score(rows, expected)
    report = {"summary": result["summary"]}
    if engine:
        report["missing_step_causes"] = dict(explain_missing(engine, requests, result))
        report["extraction_errors"] = engine.evidence.extraction_errors
    if args.details:
        report["differences"] = [c for c in result["cases"] if c["missing"] or c["extra"]
                                 or c["status"] != c["expected_status"]]
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
