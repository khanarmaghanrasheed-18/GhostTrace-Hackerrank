"""The deletion rules GhostTrace applies.

- what a request's wording asks for (deletion, or something else);
- what state each kind of data must reach under its policy action;
- how a copy's observed state is judged against that target;
- which cleanup steps follow, and in what order: findings, then remediation
  (processes are stopped before the records they feed are changed), then the
  checks that must pass afterwards;
- what a proposed what-if action would achieve.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# What the request asks for.

DELETE_WORDS = re.compile(
    r"\b(delet\w*|eras\w*|remov\w*|clos(?:e|ed|ing|ure)\b|right to be forgotten|forget me|"
    r"gone|suppression|supprim\w*|effac\w*|exclus[aã]o|exclu[ií]\w*|elimina\w*|borra\w*|"
    r"l[oö]sch\w*|disconnected)|削除|消去|탈퇴|삭제|删除", re.I)
NEGATED = re.compile(
    r"\b(?:not|n't|never|no|without)\b(?:\W+\w+){0,3}?\W+(?:delet\w*|eras\w*|remov\w*|clos\w*|"
    r"deletion|asking for deletion)", re.I)
KEEP_ACCOUNT = re.compile(
    r"\b(?:keep|leave)\b(?:\W+\w+){0,3}?\W+(?:account|profile)\b(?:\W+\w+){0,3}?\W+"
    r"(?:active|as it is|in place|open)|\bleave\b(?:\W+\w+){0,4}?\W+in place|continue using|"
    r"still (?:need|want|use)\b", re.I)
SCOPE_ONLY = re.compile(r"\bmarketing lists only\b|\bpromotional emails\b|\bopt[ -]?out\b", re.I)


def deletion_intent(text: str) -> tuple[bool | None, str]:
    """True or False when the wording decides it, None when it is genuinely unclear."""
    if NEGATED.search(text):
        return False, "deletion is explicitly declined"
    if KEEP_ACCOUNT.search(text):
        return False, "the person asks to keep using the account"
    if SCOPE_ONLY.search(text):
        return False, "marketing opt-out only"
    if DELETE_WORDS.search(text):
        return True, "explicit deletion request"
    return None, "no deletion wording found"


# ---------------------------------------------------------------------------
# The state each kind of data must reach, and how a copy is judged.

def policy_target(category: str, required: str) -> str:
    """The compliant final state for a kind of data under its policy action."""
    if category == "DATA_FLOW":
        return "DISABLED"
    if category == "BACKUP_ARCHIVE":
        return "BLOCKED"
    return {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "RETAIN": "PRESENT"}.get(required, "")


def judge(observation: Any, target: str) -> str:
    """Compare one observed state with the compliant target: ok, violation or unknown.
    observation has a state and whether it still identifies the person."""
    state = observation.state
    if state == "UNKNOWN":
        return "unknown"
    if target == "ABSENT":
        return "ok" if state == "ABSENT" else "violation"
    if target in {"REDACTED", "ANONYMIZED"}:
        if state == "ABSENT":
            return "ok"
        if observation.identifying is None:
            return "unknown"
        return "violation" if observation.identifying else "ok"
    if target == "PRESENT":
        return "ok" if state == "PRESENT" else "unknown"
    if target == "DISABLED":
        return {"DISABLED": "ok", "ACTIVE": "violation", "ABSENT": "ok"}.get(state, "unknown")
    if target == "BLOCKED":
        return {"BLOCKED": "ok", "RESTORABLE": "violation", "ABSENT": "ok"}.get(state, "unknown")
    return "unknown"


# ---------------------------------------------------------------------------
# Cleanup steps.

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

STEP_FOR_TARGET = {
    # target: (finding, remediation, verification)
    "ABSENT": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACTED": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZED": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "PRESENT": ("", "", "VERIFY_RECORD_PRESENT"),
    "DISABLED": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCKED": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}


def order_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings, then remediation, then verification; within a phase by operation
    order (so flows are disabled and restores blocked before records change),
    then by system and item. Steps are numbered from 1."""
    ordered = sorted(steps, key=lambda s: (PHASE_ORDER[s["phase"]],
                                           OPERATIONS[s["phase"]].index((s["operation"], s["state"])),
                                           s["system_id"], s["resource_id"]))
    for n, step in enumerate(ordered, 1):
        step["step_no"] = n
    return ordered


# ---------------------------------------------------------------------------
# What-if actions.

ACTION_RESULT = {"DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
                 "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED"}
ACTIONS = list(ACTION_RESULT)

# Words in a what-if question and the action they ask for.
VERBS = [
    (r"\b(?:delet\w*|remov\w*|eras\w*|purg\w*|wip\w*)\b", "DELETE_RECORD"),
    (r"\b(?:redact\w*|mask\w*|scrub\w*)\b", "REDACT_RECORD"),
    (r"\b(?:anonymi[sz]\w*|de-?identif\w*)\b", "ANONYMIZE_RECORD"),
    (r"\b(?:disabl\w*|stop\w*|switch\w* (?:it |them )?off|turn\w* (?:it |them )?off|paus\w*|disconnect\w*)\b",
     "DISABLE_DATA_FLOW"),
    (r"\bblock\w*\b", "BLOCK_RESTORE"),
]


def treatment_check(target: str, result: str) -> tuple[str, str]:
    """(outcome, reason) for an action that leaves `result` where the policy wants `target`."""
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
