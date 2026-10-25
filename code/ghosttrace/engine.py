"""Time-aware identity, evidence and policy decisions for the challenge."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from typing import Any

from .gemini import GeminiInterpreter
from .ingest import EvidenceIndex, Observation, flatten_values, iso_at, read_csv
from .validate import OPERATIONS, PHASE_ORDER


EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
DELETE_INTENT = re.compile(
    r"\b(delet(?:e|ion|ed|ing)?|eras(?:e|ure|ed)|remov(?:e|al|ed)|"
    r"clos(?:e|ed|ing) (?:my |the |this )?account|account clos(?:e|ure)|right to be forgotten|"
    r"suppression|supprim\w*|effac\w*|elimin\w*|borrar|eliminar|gel[oö]scht|l[oö]schen|"
    r"delete my data|forget me|profile (?:is |was )?gone|account (?:is |was )?gone|"
    r"confirm (?:that )?(?:my |the )?(?:profile|account) (?:is |was )?gone)\b|削除|消去|탈퇴|삭제|删除", re.I)
DENIAL = re.compile(r"\b(do not|don't|not|never|no)\s+(?:want\s+(?:you\s+to\s+)?|ask(?:ing)?\s+(?:you\s+to\s+)?)?"
                    r"(?:delete|erase|remove|close)\b", re.I)
NONDELETE = re.compile(r"\b(access|download|export|correct|update|opt[ -]?out|unsubscribe|"
                       r"marketing preferences|copy of my data)\b", re.I)
PII_KEYS = {"email", "phone", "email_hash", "full_name", "name", "display_name",
            "requester_email", "requester_phone", "requester_name", "customer_email",
            "owner_email", "owner_phone"}
NEGATIVE = {"NOT_FOUND", "ABSENT", "MISSING", "NO_MATCH", "DELETED", "REMOVED"}
POSITIVE = {"FOUND", "PRESENT", "ACTIVE", "HIT", "EXISTS"}


def normalize(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "@" in text:
        return text
    if text.startswith("+") or (text and all(c.isdigit() or c in "- ()" for c in text)):
        return "".join(c for c in text if c.isdigit())
    return text


def _is_true(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1", "yes", "enabled"}:
        return True
    if str(value).lower() in {"false", "0", "no", "disabled"}:
        return False
    return None


class IdentityResolver:
    def __init__(self, root: Path):
        self.current = read_csv(root / "dataset" / "user_identities.csv")
        self.history = read_csv(root / "dataset" / "user_audit_log.csv")
        self.by_user = {r["user_id"]: r for r in self.current}
        self.by_email: dict[str, set[str]] = defaultdict(set)
        for row in self.current:
            self.by_email[normalize(row["email"])].add(row["user_id"])
        for row in self.history:
            if row["field_name"] == "email":
                for key in ("old_value", "new_value"):
                    self.by_email[normalize(row[key])].add(row["user_id"])

    def resolve(self, email: str, cutoff: str) -> tuple[str | None, set[str], bool]:
        candidates = self.by_email.get(normalize(email), set())
        user_id = next(iter(candidates)) if len(candidates) == 1 else None
        terms = {normalize(email)}
        if not user_id:
            return None, terms, bool(candidates)
        terms.add(normalize(user_id))
        current = self.by_user.get(user_id)
        if current:
            # The current snapshot is an identity lookup aid. Its later timestamp
            # is never used to prove the state of a record at audit_at.
            for field in ("email", "phone", "email_hash"):
                if current.get(field):
                    terms.add(normalize(current[field]))
        for row in self.history:
            if row["user_id"] == user_id and iso_at(row["event_time"]) <= cutoff:
                for field in ("old_value", "new_value"):
                    if row[field]:
                        terms.add(normalize(row[field]))
        return user_id, {t for t in terms if t}, False

    def names(self, user_id: str | None) -> set[str]:
        if not user_id:
            return set()
        name = self.by_user.get(user_id, {}).get("name", "")
        return {name.lower()} if name else set()


def _record_terms(obs: Observation) -> set[str]:
    values = flatten_values(obs.data)
    terms = {normalize(v) for v in values if v and len(str(v)) < 200}
    for value in values:
        terms.update(x.lower() for x in EMAIL.findall(value))
    return terms


def _associated(obs: Observation, terms: set[str]) -> bool:
    values = _record_terms(obs)
    return bool(values & terms)


def _has_subject_pii(obs: Observation, terms: set[str], names: set[str]) -> bool:
    def check(value: Any, key: str = "") -> bool:
        if isinstance(value, dict):
            return any(check(v, k) for k, v in value.items())
        if isinstance(value, list):
            return any(check(v, key) for v in value)
        if value is None:
            return False
        raw = str(value).lower()
        if not raw or raw in {"null", "none"}:
            return False
        if key in PII_KEYS and (normalize(value) in terms or raw in names):
            return True
        if key in {"message_text", "message_excerpt", "document", "text"}:
            return any(("@" in t or len(t) >= 10) and t in raw for t in terms if not t.startswith("user_")) or any(n in raw for n in names)
        return False
    return check(obs.data)


def state(obs: Observation, terms: set[str], names: set[str]) -> str:
    d = obs.data
    category = obs.category
    result = str(d.get("result") or d.get("status") or "").upper()
    event = str(d.get("event") or "").lower()
    if event.endswith("_failed") or result in {"FAILED", "ERROR", "TIMEOUT", "PARTIAL"}:
        return "UNKNOWN"
    if d.get("status_code") in (404, "404") or d.get("http_status") in (404, "404"):
        return "ABSENT"
    if result in NEGATIVE or result in {"NOT_FOUND", "NO_HITS", "MISS"}:
        return "ABSENT"
    if result in {"ANONYMIZED", "REDACTED", "DISABLED", "BLOCKED", "IDENTIFIABLE"}:
        return "EXPOSED" if result == "IDENTIFIABLE" else result
    if str(d.get("rows")) == "0":
        return "ABSENT"
    if _is_true(d.get("available")) is False and category == "USER_EXPORT":
        return "ABSENT"
    if _is_true(d.get("available")) is True and category == "USER_EXPORT":
        return "PRESENT"
    hits = d.get("hits")
    if isinstance(hits, dict) and hits.get("total") == 0:
        return "ABSENT"
    if category == "DATA_FLOW":
        if result == "ACTIVE":
            return "ACTIVE"
        if result in {"INACTIVE", "STOPPED"}:
            return "DISABLED"
        enabled = _is_true(d.get("enabled"))
        if enabled is not None:
            return "ACTIVE" if enabled else "DISABLED"
        if str(d.get("last_delivery_status", "")).lower() == "stopped":
            return "DISABLED"
        return "UNKNOWN"
    if category == "BACKUP_ARCHIVE":
        if result in {"ACTIVE", "RESTORE_ALLOWED"}:
            return "ACTIVE"
        if result in {"RESTORE_BLOCKED", "RESTORE_DENIED"}:
            return "BLOCKED"
        allowed = _is_true(d.get("restore_allowed"))
        if allowed is not None:
            return "ACTIVE" if allowed else "BLOCKED"
        return "UNKNOWN"
    if category in {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}:
        if str(d.get("event", "")) == "analytics_identity_scan" and d.get("matched_value"):
            return "EXPOSED"
        stable = normalize(d.get("user_id") or d.get("customer_reference") or d.get("subject_reference"))
        if (stable and stable in terms) or _has_subject_pii(obs, terms, names):
            return "EXPOSED"
        if any(k in d for k in ("email", "phone", "email_hash", "customer_reference", "user_id")):
            return "ANONYMIZED"
    if category in {"SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE"}:
        if str(d.get("event", "")) in {"message_pii_scan", "attachment_pii_scan"} and d.get("matched_value"):
            return "EXPOSED"
        if _has_subject_pii(obs, terms, names):
            return "EXPOSED"
        if any(k in d for k in ("requester_email", "customer_reference", "email", "message_text", "message_excerpt")):
            return "REDACTED"
    if result in POSITIVE or obs.method in {"json", "sqlite"}:
        return "PRESENT"
    if str(d.get("rows", "")).isdigit() and int(d["rows"]) > 0:
        return "PRESENT"
    if str(d.get("event", "")).endswith("_completed") and "result" in d:
        return "PRESENT" if result in POSITIVE else "UNKNOWN"
    return "UNKNOWN"


def _store(obs: Observation) -> str:
    if obs.location.startswith("table:"):
        return obs.location.split(":")[1]
    return str(obs.data.get("data_store") or obs.data.get("collection") or obs.system_id or obs.source_path)


class AuditEngine:
    def __init__(self, root: Path, use_gemini: bool = True):
        self.root = root
        self.identity = IdentityResolver(root)
        self.evidence = EvidenceIndex.load(root)
        self.rules = {r["data_category"]: r["required_action"] for r in read_csv(root / "dataset" / "deletion_rules.csv")}
        self.gemini = GeminiInterpreter(root) if use_gemini else None
        self.activity = read_csv(root / "dataset" / "system_activity.csv")
        self.activity_by_resource: dict[str, list[dict[str, str]]] = defaultdict(list)
        for event in self.activity:
            self.activity_by_resource[event["resource_id"]].append(event)
        self._terms = [_record_terms(obs) for obs in self.evidence.observations]
        self.by_resource: dict[str, list[int]] = defaultdict(list)
        for i, obs in enumerate(self.evidence.observations):
            self.by_resource[obs.resource_id].append(i)

    def deletion_intent(self, text: str) -> tuple[bool, str]:
        if DENIAL.search(text) and not re.search(r"\b(but|instead|now)\b.*" + DELETE_INTENT.pattern, text, re.I):
            return False, "explicit denial"
        if DELETE_INTENT.search(text):
            return True, "explicit deletion request"
        if NONDELETE.search(text):
            return False, "non-deletion request"
        if self.gemini and self.gemini.ready:
            answer = self.gemini.deletion_intent(text)
            if answer is not None:
                return answer, "Gemini classified ambiguous language"
        return False, "ambiguous request without supported deletion authorization"

    def investigate(self, request: dict[str, str]) -> tuple[dict[str, str], dict[str, Any]]:
        case = request["case_id"]
        cutoff = iso_at(request["audit_at"])
        authorized, intent_reason = self.deletion_intent(request["request_text"])
        if not authorized:
            return {"case_id": case, "deletion_status": "NOT_APPLICABLE", "steps_json": "[]"}, {
                "intent": intent_reason, "resources": [], "uncertain": []}
        user_id, terms, ambiguous_identity = self.identity.resolve(request["email"], cutoff)
        names = self.identity.names(user_id)
        seed_indices = [i for i, obs in enumerate(self.evidence.observations)
                        if obs.available_at(cutoff) and bool(self._terms[i] & terms)]
        resources = {self.evidence.observations[i].resource_id for i in seed_indices}
        activity_claims = [
            {"activity_id": event["activity_id"], "event_time": event["event_time"],
             "event_type": event["event_type"], "reported_result": event["reported_result"],
             "resource_id": event["resource_id"]}
            for resource in resources for event in self.activity_by_resource.get(resource, [])
            if iso_at(event["event_time"]) <= cutoff
        ]
        activity_claims.sort(key=lambda event: (event["event_time"], event["activity_id"]))
        indices = {i for resource in resources for i in self.by_resource[resource]
                   if self.evidence.observations[i].available_at(cutoff)}
        grouped: dict[tuple[str, str], list[Observation]] = defaultdict(list)
        for i in indices:
            obs = self.evidence.observations[i]
            if obs.category and not obs.claim:
                grouped[(obs.system_id, obs.resource_id)].append(obs)
        assessed: list[dict[str, Any]] = []
        unknown: list[str] = []
        model_hints: list[dict[str, str]] = []
        findings: list[dict[str, Any]] = []
        remediation: list[dict[str, Any]] = []
        verification: list[dict[str, Any]] = []
        for (system, resource), observations in grouped.items():
            # Keep conflicting stores separate. Only the latest observation from a
            # given store can supersede that store's older observation.
            per_store: dict[str, Observation] = {}
            for obs in observations:
                store = _store(obs)
                previous = per_store.get(store)
                if previous is None or (obs.captured_at, obs.observed_at) > (previous.captured_at, previous.observed_at):
                    per_store[store] = obs
            candidates = list(per_store.values())
            category = max(candidates, key=lambda o: (o.captured_at, o.observed_at)).category
            required = self.rules.get(category)
            if category == "DATA_FLOW":
                required = "DISABLE"
            if not required:
                continue
            states = [(obs, state(obs, terms, names)) for obs in candidates]
            if category == "SECURITY_AUDIT_LOG":
                states = [(obs, s) for obs, s in states if not obs.data.get("retention_until") or
                          iso_at(obs.data["retention_until"]) >= cutoff]
                if not states:
                    continue
            violations = self._violations(category, required, states)
            chosen = max(violations or states, key=lambda pair: (pair[0].captured_at, pair[0].observed_at), default=None)
            if chosen is None:
                continue
            obs, observed_state = chosen
            assessed.append({"system_id": system, "resource_id": resource, "category": category,
                             "required": required, "state": observed_state, "evidence_id": obs.evidence_id,
                             "observations": len(observations), "conflict": len({s for _, s in states}) > 1})
            if observed_state == "UNKNOWN":
                unknown.append(resource)
                if self.gemini and self.gemini.ready and obs.method in {"ocr", "pdf_text", "pdf_ocr"}:
                    excerpt = str(obs.data.get("raw_line") or obs.data.get("text") or "")
                    if excerpt:
                        hint = self.gemini.interpret_excerpt(excerpt)
                        if hint:
                            model_hints.append({"resource_id": resource, "hint": hint,
                                                "source_path": obs.source_path, "evidence_id": obs.evidence_id})
                continue
            if violations:
                finding, remedy, verify = self._actions(category, required, observed_state)
                if finding:
                    findings.append(self._step("FINDING", system, resource, finding, obs.evidence_id))
                if remedy:
                    remediation.append(self._step("REMEDIATION", system, resource, remedy, ""))
                if verify:
                    verification.append(self._step("VERIFICATION", system, resource, verify, ""))
            else:
                verify = self._verification(category, required)
                if verify:
                    verification.append(self._step("VERIFICATION", system, resource, verify, obs.evidence_id))
        incomplete = bool(findings or unknown or ambiguous_identity or not assessed or self.evidence.extraction_errors)
        if incomplete:
            for step in verification:
                step["evidence_id"] = ""
        else:
            findings.clear()
            remediation.clear()
        steps = findings + remediation + verification
        steps.sort(key=lambda s: (PHASE_ORDER[s["phase"]],
                                  OPERATIONS[s["phase"]].index((s["operation"], s["state"])),
                                  s["system_id"], s["resource_id"]))
        # A resource can be seen in multiple sources; emit one operation per resource.
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for step in steps:
            key = (step["phase"], step["operation"], step["resource_id"])
            if key not in seen:
                unique.append(step)
                seen.add(key)
        for n, step in enumerate(unique, 1):
            step["step_no"] = n
        row = {"case_id": case, "deletion_status": "INCOMPLETE" if incomplete else "COMPLETE",
               "steps_json": json.dumps(unique, separators=(",", ":"), ensure_ascii=False)}
        return row, {"intent": intent_reason, "user_id": user_id, "resources": assessed,
                     "uncertain": unknown, "ambiguous_identity": ambiguous_identity,
                     "matched_observations": len(indices), "model_hints": model_hints,
                     "activity_claims": activity_claims}

    @staticmethod
    def _violations(category: str, required: str, states: list[tuple[Observation, str]]) -> list[tuple[Observation, str]]:
        if category == "BACKUP_ARCHIVE":
            return [(o, s) for o, s in states if s == "ACTIVE"]
        if category == "DATA_FLOW":
            return [(o, s) for o, s in states if s == "ACTIVE"]
        expected = {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "RETAIN": "PRESENT"}.get(required)
        return [(o, s) for o, s in states if s not in {expected, "UNKNOWN"}]

    @staticmethod
    def _actions(category: str, required: str, observed: str) -> tuple[str, str, str]:
        if category == "DATA_FLOW":
            return "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"
        if category == "BACKUP_ARCHIVE":
            return "REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"
        if required == "DELETE":
            return "REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"
        if required == "REDACT":
            return "REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"
        if required == "ANONYMIZE":
            return "REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"
        return "", "", "VERIFY_RECORD_PRESENT"

    @staticmethod
    def _verification(category: str, required: str) -> str:
        if category == "DATA_FLOW":
            return "VERIFY_DATA_FLOW_DISABLED"
        if category == "BACKUP_ARCHIVE":
            return "VERIFY_RESTORE_BLOCKED"
        return {"DELETE": "VERIFY_RECORD_ABSENT", "REDACT": "VERIFY_RECORD_REDACTED",
                "ANONYMIZE": "VERIFY_RECORD_ANONYMIZED", "RETAIN": "VERIFY_RECORD_PRESENT"}.get(required, "")

    @staticmethod
    def _step(phase: str, system: str, resource: str, operation: str, evidence: str) -> dict[str, Any]:
        state_value = next(state for op, state in OPERATIONS[phase] if op == operation)
        return {"step_no": 0, "phase": phase, "system_id": system, "resource_id": resource,
                "operation": operation, "state": state_value, "evidence_id": evidence}
