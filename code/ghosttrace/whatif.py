"""Hypothetical cleanup analysis for one case.

A scenario is a list of proposed actions. They are applied to a temporary copy
of the case model built from the audit engine's result at audit_at, and the copy
is re-checked for remaining obligations, retention conflicts and recreation
paths. Nothing here writes to the dataset, the audit result or output.csv, and
nothing is ever sent to a live system. Scenarios are stored under
.cache/ghosttrace/whatif/ so proposals can accumulate between commands.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
from typing import Any

from .ingest import RESOURCE, iso_at
from .views import recreation_paths

HYPOTHETICAL = ("HYPOTHETICAL RESULT - limited to the supplied evidence at audit_at. "
                "No system was changed and the audit verdict and output.csv are unaffected.")

# What each action would leave behind.
ACTION_RESULT = {"DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
                 "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED"}
ACTIONS = list(ACTION_RESULT)

VERBS = [
    (r"\b(?:delet\w*|remov\w*|eras\w*|purg\w*|wip\w*)\b", "DELETE_RECORD"),
    (r"\b(?:redact\w*|mask\w*|scrub\w*)\b", "REDACT_RECORD"),
    (r"\b(?:anonymi[sz]\w*|de-?identif\w*)\b", "ANONYMIZE_RECORD"),
    (r"\b(?:disabl\w*|stop\w*|switch\w* (?:it |them )?off|turn\w* (?:it |them )?off|paus\w*|disconnect\w*)\b",
     "DISABLE_DATA_FLOW"),
    (r"\bblock\w*\b", "BLOCK_RESTORE"),
]
NOUNS = [
    (r"\b(?:accounts?|profiles?)\b", "account_"), (r"\bsearch\w*|\bindex\w*", "search_"),
    (r"\banalytics\b", "analytics_"), (r"\b(?:support|tickets?|messages?|attachments?)\b", "support_"),
    (r"\b(?:exports?|downloads?)\b", "export_"),
    (r"\b(?:flows?|connectors?|integrations?|sync\w*|bridges?)\b", "data_flow_"),
    (r"\b(?:backups?|archives?|restores?)\b", "backup_"), (r"\b(?:audit|security)\b", "audit_record_"),
    (r"\btombstones?\b|deleted.account reference", "tombstone_"), (r"\bmarketing\b", "marketing_"),
]


class CaseModel:
    """The case as the audit engine judged it, plus a hypothetical overlay."""

    def __init__(self, engine: Any, request: dict[str, str]):
        self.engine = engine
        self.request = request
        self.case_id = request["case_id"]
        self.cutoff = iso_at(request["audit_at"])
        row, detail = engine.investigate(request)
        # Deep copies: the scenario can never write into the engine's result.
        self.audit_row = dict(row)
        self.detail = copy.deepcopy(detail)
        self.records = {r["resource_id"]: r for r in self.detail.get("resources", [])}

    def original_state(self, resource: str) -> str:
        """ok, violation, unknown or unobserved: the engine's verdict at audit_at."""
        stores = self.records[resource]["stores"]
        if not stores:
            return "unobserved"
        verdicts = {info["verdict"] for info in stores.values()}
        if "violation" in verdicts:
            return "violation"
        return "ok" if verdicts == {"ok"} else "unknown"

    def candidates(self, prefix: str = "", action: str = "") -> list[str]:
        found = []
        for resource in sorted(self.records):
            if prefix and not resource.startswith(prefix):
                continue
            if action and validate_type(action, resource):
                continue
            found.append(resource)
        return found


def validate_type(action: str, resource: str) -> str:
    """An error message when the action cannot apply to this kind of resource."""
    if action not in ACTION_RESULT:
        return f"unsupported action {action}"
    if action == "DISABLE_DATA_FLOW" and not resource.startswith("data_flow_"):
        return "only a data flow can be disabled"
    if action == "BLOCK_RESTORE" and not resource.startswith("backup_"):
        return "only a backup archive can have restore blocked"
    if resource.startswith("data_flow_") and action != "DISABLE_DATA_FLOW":
        return "a data flow is a process; the supported action is DISABLE_DATA_FLOW"
    if resource.startswith("backup_") and action in ("REDACT_RECORD", "ANONYMIZE_RECORD"):
        return "an archive cannot be redacted or anonymized in place; block restore instead"
    return ""


def validate(model: CaseModel, action: str, resource: str | None) -> dict[str, Any]:
    """Accept, reject or ask for clarification on one proposed action."""
    action = (action or "").upper()
    if action not in ACTION_RESULT:
        return {"status": "rejected", "action": action, "resource_id": resource,
                "reason": f"unsupported action; allowed: {', '.join(ACTIONS)}"}
    if not resource:
        options = model.candidates(action=action)
        return {"status": "clarify", "action": action, "resource_id": None, "candidates": options,
                "reason": "the proposal does not say which record"}
    if resource not in model.records:
        known = resource in model.engine.by_resource or any(
            e["resource_id"] == resource for e in model.engine.activity)
        reason = ("exists in the supplied data but is not linked to this person at audit_at" if known
                  else "does not exist in the supplied evidence")
        return {"status": "rejected", "action": action, "resource_id": resource, "reason": reason}
    problem = validate_type(action, resource)
    if problem:
        return {"status": "rejected", "action": action, "resource_id": resource, "reason": problem}
    return {"status": "accepted", "action": action, "resource_id": resource}


def parse_action(text: str) -> tuple[str, str | None]:
    """'DELETE_RECORD search_1993' -> ('DELETE_RECORD', 'search_1993')."""
    parts = text.replace(":", " ").replace("=", " ").split()
    action = parts[0].upper() if parts else ""
    resource = parts[1] if len(parts) > 1 else None
    return action, resource


# ---------------------------------------------------------------------------
# Natural-language questions.

def interpret(model: CaseModel, question: str, gemini: Any = None) -> dict[str, Any]:
    """Turn a question into validated proposals, clarifications and rejections."""
    if gemini is not None and gemini.ready:
        listing = [{"resource_id": r, "system_id": rec["system_id"], "category": rec["category"]}
                   for r, rec in sorted(model.records.items())]
        answer = gemini.propose_actions(question, listing, ACTIONS)
        if isinstance(answer, dict) and isinstance(answer.get("actions"), list):
            return checked(model, answer["actions"], f"gemini:{gemini.model}")
        reason = "Gemini returned no usable proposal"
    else:
        reason = "Gemini unavailable (GEMINI_API_KEY not configured)"
    return checked(model, local_rules(model, question), f"local-rules ({reason})")


def local_rules(model: CaseModel, question: str) -> list[dict[str, Any]]:
    """A small rule-based reader used when Gemini is not available.

    It recognizes action verbs and record kinds, and explicit resource IDs. It
    never picks between several matching records; that becomes a clarification.
    """
    proposals: list[dict[str, Any]] = []
    action = ""
    clauses = re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b|\balso\b|\bplus\b)\s*", question, flags=re.I)
    for clause in clauses:
        if not clause.strip():
            continue
        for pattern, verb in VERBS:
            if re.search(pattern, clause, re.I):
                action = verb
                break
        if not action:
            continue
        ids = RESOURCE.findall(clause)
        if ids:
            proposals.extend({"action": action, "resource_id": r, "candidates": []} for r in ids)
            continue
        prefixes = [prefix for pattern, prefix in NOUNS if re.search(pattern, clause, re.I)]
        if action == "BLOCK_RESTORE":
            prefixes = ["backup_"]
        if not prefixes:
            options = model.candidates(action=action)
            if len(options) == 1:
                proposals.append({"action": action, "resource_id": options[0], "candidates": []})
            else:
                proposals.append({"action": action, "resource_id": None, "candidates": options})
            continue
        for prefix in prefixes:
            options = model.candidates(prefix=prefix)
            if len(options) == 1:
                proposals.append({"action": action, "resource_id": options[0], "candidates": []})
            elif options:
                proposals.append({"action": action, "resource_id": None, "candidates": options})
            else:
                proposals.append({"action": action, "resource_id": None, "candidates": [],
                                  "missing": prefix[:-1]})
    return proposals


def checked(model: CaseModel, proposals: list[Any], interpreter: str) -> dict[str, Any]:
    """Apply the same validation to every proposal, whoever made it."""
    result: dict[str, Any] = {"interpreter": interpreter, "accepted": [], "clarify": [], "rejected": []}
    for item in proposals:
        if not isinstance(item, dict):
            result["rejected"].append({"proposal": item, "reason": "not an action object"})
            continue
        action = str(item.get("action") or "")
        resource = item.get("resource_id")
        if item.get("missing"):
            result["rejected"].append({"status": "rejected", "action": action, "resource_id": None,
                                       "reason": f"this case has no {item['missing']} record at audit_at"})
            continue
        candidates = [c for c in item.get("candidates") or [] if c in model.records]
        if not resource and len(candidates) == 1:
            resource = candidates[0]
        check = validate(model, action, resource)
        if check["status"] == "clarify" and candidates:
            check["candidates"] = candidates
        if check["status"] == "clarify":
            check["question"] = (f"Which record do you mean for {check['action']}? "
                                 f"Options: {', '.join(check['candidates']) or 'none in this case'}")
        result[check["status"]].append(check)
    if not proposals:
        result["clarify"].append({"status": "clarify", "action": None, "resource_id": None, "candidates": [],
                                  "question": "No supported action was recognized. Try for example "
                                              "'disable the connector and delete the search record'."})
    return result


# ---------------------------------------------------------------------------
# Applying and assessing a scenario.

def treatment_check(target: str, result: str) -> tuple[str, str]:
    """(outcome, reason) for applying an action that leaves `result` where policy wants `target`."""
    if result == target:
        return "resolved", f"would be {result}, as policy requires"
    if target == "REDACTED" and result == "ANONYMIZED":
        return "resolved", "would be ANONYMIZED, which also removes the direct identifiers policy requires removed"
    if result == "ABSENT" and target in ("PRESENT", "REDACTED", "ANONYMIZED", "BLOCKED"):
        keep = {"PRESENT": "retained", "REDACTED": "kept with identifiers redacted",
                "ANONYMIZED": "kept in anonymized form", "BLOCKED": "retained with restore blocked"}[target]
        return "policy conflict", f"policy requires this record to be {keep}; deleting it would violate that"
    if target == "PRESENT":
        return "policy conflict", f"policy requires this record to be retained unchanged, not {result}"
    if target == "ABSENT":
        return "still required", f"would be {result}, but policy requires the whole record to be deleted"
    if target == "ANONYMIZED" and result == "REDACTED":
        return "still required", "redaction can leave a stable reference; policy requires anonymization"
    return "still required", f"would be {result}; policy requires {target}"


def assess(model: CaseModel, actions: list[dict[str, str]]) -> dict[str, Any]:
    """Re-check a hypothetical copy of the case after the proposed actions."""
    applied: dict[str, str] = {}
    for item in actions:
        applied[item["resource_id"]] = ACTION_RESULT[item["action"]]
    rules = model.engine.rules
    outcome: dict[str, list[dict[str, str]]] = {"resolved": [], "still required": [], "policy conflict": [],
                                                 "compliant at audit_at": [], "unknown state": [],
                                                 "verify after cleanup": []}
    for resource, record in sorted(model.records.items()):
        rule = rules.get(record["category"], {})
        entry = {"resource_id": resource, "system_id": record["system_id"], "category": record["category"],
                 "rule_id": rule.get("rule_id", ""), "policy": record["required"], "required": record["target"]}
        original = model.original_state(resource)
        if resource in applied:
            status, reason = treatment_check(record["target"], applied[resource])
            if status == "resolved" and original == "ok":
                reason += " (it was already compliant at audit_at)"
            outcome[status].append({**entry, "reason": reason})
        elif original == "violation":
            outcome["still required"].append({**entry, "reason": "the audit found it non-compliant and no "
                                                                 "proposed action treats it"})
        elif original == "unknown":
            outcome["unknown state"].append({**entry, "reason": "its state at audit_at is not established"})
        elif original == "unobserved":
            outcome["verify after cleanup"].append({**entry, "reason": "no evidence at or before audit_at"})
        else:
            outcome["compliant at audit_at"].append({**entry, "reason": "already compliant"})
    # Recreation: processes the scenario closes are overridden; the rest stay as observed.
    overrides = {r: applied[r] for r in applied if r.startswith(("data_flow_", "backup_"))}
    paths = recreation_paths(model.engine, model.detail, model.cutoff, overrides)
    changed = [r for r in applied if not r.startswith(("data_flow_", "backup_"))]
    for path in paths:
        path["would_recreate_changed_records"] = sorted(set(path["can_recreate"]) & set(changed))
    # Order: changing a record while a process that can rewrite it is still open.
    warnings = []
    open_paths = {p["process"]: p for p in recreation_paths(model.engine, model.detail, model.cutoff)}
    for item in actions:
        resource = item["resource_id"]
        if resource in open_paths:
            open_paths.pop(resource)
            continue
        for process, path in open_paths.items():
            if resource in path["can_recreate"]:
                warnings.append(f"{resource} is changed before {process} is closed; close the "
                                f"{path['kind']} path first or the change can be undone")
    complete = not (outcome["still required"] or outcome["policy conflict"] or outcome["unknown state"] or paths)
    return {"case_id": model.case_id, "audit_at": model.cutoff, "label": HYPOTHETICAL,
            "audit_verdict_unchanged": model.audit_row["deletion_status"],
            "actions": actions, "outcome": outcome, "recreation_paths": paths, "order_warnings": warnings,
            "hypothetical_status": "COMPLETE" if complete else "INCOMPLETE",
            "not_modelled": {
                "workflow_events_tied_with_other_requests": model.detail.get("unassigned_workflow_resources", []),
                "note": "Records the evidence does not tie to this person are outside the model."}}


def explain(result: dict[str, Any]) -> str:
    """Plain-text explanation built only from the checked assessment."""
    out = [result["label"],
           f"Case {result['case_id']} at {result['audit_at']}; audit verdict stays "
           f"{result['audit_verdict_unchanged']}.",
           "Proposed actions: " + (", ".join(f"{a['action']} {a['resource_id']}" for a in result["actions"])
                                    or "none")]
    status = result["hypothetical_status"]
    out.append(f"If carried out, the case would be {status} within the supplied evidence.")
    labels = {"resolved": "Resolved", "policy conflict": "Policy conflict", "still required": "Still required",
              "unknown state": "Unknown", "verify after cleanup": "Verify after cleanup",
              "compliant at audit_at": "Already compliant"}
    for key, label in labels.items():
        for entry in result["outcome"][key]:
            rule = f" [{entry['rule_id']} {entry['category']} -> {entry['policy']}]" if entry["rule_id"] else ""
            out.append(f"- {label}: {entry['system_id']}/{entry['resource_id']}{rule}: {entry['reason']}")
    for path in result["recreation_paths"]:
        where = ", ".join(path.get("destination_systems") or []) or path["destination_basis"]
        line = (f"- Recreation risk: {path['process']} is {path['state']} per {path['state_evidence']} "
                f"and can write to {where}")
        if path.get("destination_systems"):
            line += f" ({path['destination_basis']})"
        if path["would_recreate_changed_records"]:
            line += f"; it could recreate {', '.join(path['would_recreate_changed_records'])} after cleanup"
        out.append(line)
    for warning in result["order_warnings"]:
        out.append(f"- Order: {warning}")
    if result["order_warnings"] and status == "COMPLETE":
        out.append("  The final state is compliant only if processes are closed before the records they feed change.")
    tied = result["not_modelled"]["workflow_events_tied_with_other_requests"]
    if tied:
        out.append(f"- Not modelled: {', '.join(tied)} (workflow events tied with other requests)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Scenario storage, outside the dataset and the submission output.

def scenario_path(root: Path, case_id: str) -> Path:
    return root / ".cache" / "ghosttrace" / "whatif" / f"{case_id}.json"


def load_scenario(root: Path, case_id: str) -> list[dict[str, str]]:
    path = scenario_path(root, case_id)
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("actions", [])


def save_scenario(root: Path, case_id: str, actions: list[dict[str, str]]) -> None:
    path = scenario_path(root, case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"case_id": case_id, "label": "hypothetical", "actions": actions}, indent=2),
                    encoding="utf-8")


def reset_scenario(root: Path, case_id: str) -> None:
    path = scenario_path(root, case_id)
    if path.is_file():
        path.unlink()


def add_actions(actions: list[dict[str, str]], accepted: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Append accepted proposals; a later action on the same record replaces the earlier one."""
    result = list(actions)
    for item in accepted:
        result = [a for a in result if a["resource_id"] != item["resource_id"]]
        result.append({"action": item["action"], "resource_id": item["resource_id"]})
    return result
