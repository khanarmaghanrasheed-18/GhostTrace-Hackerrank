"""Public-sample evaluation; expected answers never enter prediction code."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from ghosttrace.engine import AuditEngine  # noqa: E402
from ghosttrace.ingest import read_csv  # noqa: E402


def main() -> int:
    requests = read_csv(ROOT / "dataset/sample_deletion_requests.csv")
    expected = {r["case_id"]: r for r in read_csv(ROOT / "dataset/sample_output.csv")}
    engine = AuditEngine(ROOT, use_gemini=False)
    totals = {"cases": len(requests), "status_correct": 0, "exact_steps": 0,
              "expected_steps": 0, "predicted_steps": 0, "matching_steps": 0,
              "matching_fields": 0, "expected_fields": 0}
    failures = []
    for request in requests:
        actual, detail = engine.investigate(request)
        gold = expected[request["case_id"]]
        pred_steps = json.loads(actual["steps_json"])
        gold_steps = json.loads(gold["steps_json"])
        totals["status_correct"] += actual["deletion_status"] == gold["deletion_status"]
        totals["exact_steps"] += pred_steps == gold_steps
        totals["expected_steps"] += len(gold_steps)
        totals["predicted_steps"] += len(pred_steps)
        gold_set = {json.dumps(s,sort_keys=True) for s in gold_steps}
        totals["matching_steps"] += sum(json.dumps(s,sort_keys=True) in gold_set for s in pred_steps)
        totals["expected_fields"] += len(gold_steps) * 7
        totals["matching_fields"] += sum(
            sum(p.get(k) == g.get(k) for k in g)
            for p, g in zip(pred_steps, gold_steps))
        if pred_steps != gold_steps or actual["deletion_status"] != gold["deletion_status"]:
            failures.append({"case_id": request["case_id"], "status": actual["deletion_status"],
                             "expected_status": gold["deletion_status"], "predicted_steps": len(pred_steps),
                             "expected_steps": len(gold_steps), "matched_resources": len(detail.get("resources", []))})
    print(json.dumps({**totals, "failures": failures, "extraction_errors": engine.evidence.extraction_errors}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
