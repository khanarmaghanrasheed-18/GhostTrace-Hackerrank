"""Time-aware identity, evidence and policy decisions for each deletion request.

The engine works in four steps for a case:
1. decide whether the request authorizes deletion;
2. find every resource that belongs to the person (the ownership graph);
3. reconstruct each resource's state in each store at audit_at;
4. apply the supplied rule for its data category and write the steps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any

from .gemini import GeminiInterpreter
from .graph import EvidenceGraph
from .identity import IdentityIndex
from .ingest import (EMAIL, PHONE, RESOURCE, EvidenceIndex, Observation, default_category,
                     default_system, iso_at, normalize, read_csv)
from .validate import OPERATIONS, PHASE_ORDER


# ---------------------------------------------------------------------------
# Request intent.

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
    """True/False when the wording decides it, None when it is genuinely unclear."""
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
# Policy: what each data category must look like when deletion is complete.

def policy_target(category: str, required: str) -> str:
    """The compliant final state for a category under its supplied rule."""
    if category == "DATA_FLOW":
        return "DISABLED"
    if category == "BACKUP_ARCHIVE":
        return "BLOCKED"
    return {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED",
            "RETAIN": "PRESENT"}.get(required, "")


STEP_FOR_TARGET = {
    # target: (finding, remediation, verification)
    "ABSENT": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACTED": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZED": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "PRESENT": ("", "", "VERIFY_RECORD_PRESENT"),
    "DISABLED": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCKED": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}


def judge(observation: Observation, target: str) -> str:
    """Compare one observed state with the compliant target: ok, violation or unknown."""
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


def order_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings, then remediation, then verification; within a phase by operation
    order (so flows are disabled and restores blocked before records change),
    then system_id and resource_id. Steps are renumbered from 1."""
    ordered = sorted(steps, key=lambda s: (PHASE_ORDER[s["phase"]],
                                           OPERATIONS[s["phase"]].index((s["operation"], s["state"])),
                                           s["system_id"], s["resource_id"]))
    for n, step in enumerate(ordered, 1):
        step["step_no"] = n
    return ordered


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(iso_at(value).replace("Z", "+00:00"))


def resource_type(resource: str) -> str:
    return resource.rsplit("_", 1)[0]


# A workflow run performs its operations shortly before it reports a status,
# and final results from downstream systems can arrive a couple of hours later.
WORKFLOW_BEFORE = timedelta(hours=2)
WORKFLOW_AFTER = timedelta(hours=2, minutes=30)
NEAREST_MARGIN = timedelta(minutes=30)
RUN_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
USER_REFERENCE = re.compile(r"\buser_\d+\b")
CATALOG_LINK = re.compile(r"linked (\S+) \(([^)]*)\) to (\S+) and (user_\d+)")
CATALOG_ONLY = re.compile(r"confirmed (\S+) \(([^)]*)\) belongs only to (\S+) and (user_\d+)")
RESTORED_INTO = re.compile(r"repopulated downstream resource (\S+?)\.?$")
CHECK_STATES = {"CHECK_DATA_FLOW": "ACTIVE", "CHECK_RESTORE_ACCESS": "RESTORABLE"}


class AuditEngine:
    def __init__(self, root: Path, use_gemini: bool = True):
        self.root = root
        self.identity = IdentityIndex(root)
        self.evidence = EvidenceIndex.load(root)
        self.rules = {r["data_category"]: r for r in read_csv(root / "dataset" / "deletion_rules.csv")}
        self.gemini = GeminiInterpreter(root) if use_gemini else None
        self.activity = read_csv(root / "dataset" / "system_activity.csv")
        self.activity_by_id = {e["activity_id"]: e for e in self.activity}
        self.evidence_files = {r["file_id"]: r for r in read_csv(root / "dataset" / "evidence_files.csv")}
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        for obs in self.evidence.observations:
            self.by_resource[obs.resource_id].append(obs)
        self.aliases: dict[str, str] = {}
        self.owners: dict[str, set[str]] = defaultdict(set)
        self.owner_sources: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._build_ownership()
        self._add_activity_observations()
        self._add_document_observations()
        self.file_terms: dict[str, set[str]] = defaultdict(set)
        for obs in self.evidence.observations:
            self.file_terms[obs.evidence_id].update(obs.keys)
        self.activity_time = {e["activity_id"]: iso_at(e["event_time"]) for e in self.activity}
        # First and last time evidence or an explicit link mentions each resource.
        # Routine activity is left out: it often belongs to someone else's workflow.
        self.first_seen: dict[str, str] = {}
        self.last_seen: dict[str, str] = defaultdict(str)
        times: dict[str, list[str]] = defaultdict(list)
        for obs in self.evidence.observations:
            times[obs.resource_id].append(obs.time())
        for (resource, _), sources in self.owner_sources.items():
            times[resource].extend(self.activity_time[s] for s in sources if s in self.activity_time)
        for resource, seen in times.items():
            self.first_seen[resource] = min(seen)
            self.last_seen[resource] = max(seen)
        self.assigned: dict[str, dict[str, str]] = {}
        self.cycles: dict[str, list[dict[str, str]]] = defaultdict(list)
        self._assign_workflow_events()
        # The evidence graph holds every link above with its provenance; the
        # case investigation reads identities and record links from it.
        self.graph = EvidenceGraph.build(self)
        for case, resources in self.assigned.items():
            self.graph.add_workflow_links(case, resources, self.activity_time)

    # -- ownership graph ----------------------------------------------------

    def _link(self, resource: str, user: str, source: str) -> None:
        self.owners[resource].add(user)
        self.owner_sources[(resource, user)].add(source)

    def _build_ownership(self) -> None:
        """Connect resources to people only through identifiers or explicit links."""
        for obs in self.evidence.observations:
            for key in obs.keys:
                for user in self.identity.users_for(key):
                    self._link(obs.resource_id, user, obs.evidence_id)
        for event in self.activity:
            if event["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            match = CATALOG_LINK.search(event["details"]) or CATALOG_ONLY.search(event["details"])
            if not match:
                continue
            resource, alias, _, user = match.groups()
            self._link(resource, user, event["activity_id"])
            if alias and alias != resource:
                self.aliases[alias] = resource
        # A restore copies the archive owner's data into the restored resource.
        for event in self.activity:
            match = RESTORED_INTO.search(event["details"]) if event["event_type"] == "RESTORE_BACKUP" else None
            if match:
                for user in set(self.owners.get(event["resource_id"], set())):
                    self._link(match.group(1), user, event["activity_id"])

    def _add_activity_observations(self) -> None:
        """Status checks report an observed state; other workflow events are only claims."""
        for event in self.activity:
            state = CHECK_STATES.get(event["event_type"])
            if not state or event["reported_result"] != "SUCCESS":
                continue
            resource = event["resource_id"]
            obs = Observation(
                evidence_id=event["activity_id"], source_path="system_activity.csv",
                location=f"activity:{event['activity_id']}", captured_at=iso_at(event["event_time"]),
                observed_at=iso_at(event["event_time"]), system_id=default_system(resource),
                resource_id=resource, store="primary", category=default_category(resource),
                state=state, identifying=None, fields={"details": event["details"]}, method="activity")
            self.evidence.observations.append(obs)
            self.by_resource[resource].append(obs)

    def _add_document_observations(self) -> None:
        """Find resources named in PDFs and screenshots, allowing for OCR spacing errors."""
        known = set(self.by_resource) | {e["resource_id"] for e in self.activity} | set(self.owners)
        loose = re.compile(r"\b(" + "|".join(sorted({resource_type(r) for r in known if RESOURCE.fullmatch(r)},
                                                    key=len, reverse=True)) + r")[\s._]?(\d+)\b")
        for doc in self.evidence.documents:
            if doc.method not in {"pdf_text", "pdf_ocr", "ocr"} or not doc.text:
                continue
            found: dict[str, str] = {}
            for resource in RESOURCE.findall(doc.text):
                found[resource] = "resource ID in text"
            # OCR can turn the underscore into a dot or space; accept that only
            # for IDs that other evidence or activity already names.
            for prefix, number in loose.findall(doc.text):
                resource = f"{prefix}_{number}"
                if resource in known:
                    found.setdefault(resource, "resource ID in OCR text")
            flat = re.sub(r"[\W_]+", "", doc.text.lower())
            people = set(USER_REFERENCE.findall(doc.text))
            for alias, resource in self.aliases.items():
                # A user reference names a record only in a document about that one person.
                if USER_REFERENCE.fullmatch(alias) and people != {alias}:
                    continue
                stem = re.sub(r"\.[a-z]{3}$", "", alias.lower())
                pattern = re.sub(r"[\W_]+", "", stem)
                if pattern and re.search(re.escape(pattern) + r"(?!\d)", flat):
                    found.setdefault(resource, f"catalog alias {alias}")
            emails = {normalize(e) for e in EMAIL.findall(doc.text)}
            phones = {normalize(p) for p in PHONE.findall(doc.text)}
            # Only a document about a single resource ties that resource to the
            # people it names; a list of many records does not.
            keys = (emails | phones) if len(found) == 1 else set()
            for resource, how in found.items():
                category = default_category(resource)
                if resource.startswith("support_") and doc.method.startswith("pdf"):
                    category = "SUPPORT_ATTACHMENT"   # a stored document is an attachment
                state = "PRESENT"
                if resource.startswith("data_flow_"):
                    state = "DISABLED" if re.search(r"\b(disabled|paused|stopped)\b", doc.text, re.I) else "ACTIVE"
                elif resource.startswith("backup_"):
                    # A listed archive holding the person's data is a restore path
                    # unless the page shows that restore is blocked.
                    blocked = re.search(r"restore (?:blocked|denied|disabled)|legal block|deletion block", doc.text, re.I)
                    state = "BLOCKED" if blocked else "RESTORABLE"
                obs = Observation(
                    evidence_id=doc.evidence_id, source_path=doc.source_path, location=how,
                    captured_at=doc.captured_at, observed_at=doc.captured_at,
                    system_id=default_system(resource), resource_id=resource, store="primary",
                    category=category, state=state, identifying=bool(emails or phones),
                    keys=keys, fields={}, method=doc.method)
                self.evidence.observations.append(obs)
                self.by_resource[resource].append(obs)
                for key in keys:
                    for user in self.identity.users_for(key):
                        self._link(resource, user, doc.evidence_id)

    # -- workflow events ----------------------------------------------------

    def _requests(self) -> list[dict[str, str]]:
        rows: dict[str, dict[str, str]] = {}
        for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
            path = self.root / "dataset" / name
            if path.is_file():
                for row in read_csv(path):
                    rows[row["case_id"]] = row
        return list(rows.values())

    def _assign_workflow_events(self) -> None:
        """Attach deletion-worker events to the request whose workflow produced them.

        Events carry no person, so a resource is attached only when the evidence
        already ties it to the person, or when exactly one request can still
        claim it after these eliminations:
        - a resource the evidence ties to someone else is not claimable;
        - a run touches at most one resource of each type;
        - a primary-profile result must agree with the status that run reported;
        - a contested event goes to the run whose report is clearly nearest in time.
        Ties that remain are left unassigned rather than guessed.
        """
        worker = [e for e in self.activity if e["source_system"] == "deletion_worker"]
        subjects: dict[str, str] = {}
        candidates: dict[str, dict[str, str]] = {}
        for request in self._requests():
            authorized, _ = deletion_intent(request["request_text"])
            user, _ = self.identity.resolve(request["email"], request["requested_at"])
            if authorized is False or not user:
                continue
            subjects[request["case_id"]] = user
            self.cycles[user].append(request)
            low = max(parse_time(request["requested_at"]), parse_time(request["reported_at"]) - WORKFLOW_BEFORE)
            high = parse_time(request["reported_at"]) + WORKFLOW_AFTER
            mine: dict[str, str] = {}
            for event in worker:
                when = parse_time(event["event_time"])
                if not low <= when <= high:
                    continue
                # The status a workflow reports is the outcome of its own primary
                # profile operation. Used only to match a claim to its run, never
                # as proof of the resulting state.
                if ("submitted primary profile" in event["details"]
                        and RUN_RESULT.get(request["reported_status"]) not in (None, event["reported_result"])):
                    continue
                mine.setdefault(event["resource_id"], event["activity_id"])
            candidates[request["case_id"]] = mine
        assigned: dict[str, dict[str, str]] = {case: {} for case in candidates}
        open_claims: dict[str, dict[str, str]] = {case: {} for case in candidates}
        for case, mine in candidates.items():
            for resource, activity_id in mine.items():
                owners = self.owners.get(resource, set())
                if subjects[case] in owners:
                    assigned[case][resource] = activity_id
                elif not owners:
                    open_claims[case][resource] = activity_id
        reported = {r["case_id"]: parse_time(r["reported_at"]) for r in self._requests()}
        event_time = {e["activity_id"]: parse_time(e["event_time"]) for e in worker}
        changed = True
        while changed:
            changed = self._settle_claims(assigned, open_claims)
            # When several runs could have produced an event, attribute it to the
            # run whose reported status is clearly the nearest in time.
            claimants: dict[str, list[str]] = defaultdict(list)
            for case, claims in open_claims.items():
                for resource in claims:
                    claimants[resource].append(case)
            for resource, cases in claimants.items():
                if len(cases) < 2:
                    continue
                gaps = sorted((abs(event_time[open_claims[c][resource]] - reported[c]), c) for c in cases)
                if gaps[1][0] - gaps[0][0] >= NEAREST_MARGIN:
                    winner = gaps[0][1]
                    assigned[winner][resource] = open_claims[winner].pop(resource)
                    for case in cases:
                        open_claims[case].pop(resource, None)
                    changed = True
                    break
        self.assigned = assigned
        self.unresolved_claims = open_claims

    @staticmethod
    def _settle_claims(assigned: dict[str, dict[str, str]], open_claims: dict[str, dict[str, str]]) -> bool:
        """Apply the one-resource-per-type and single-claimant rules until stable."""
        any_change = False
        changed = True
        while changed:
            changed = False
            # A workflow run touches at most one resource of each type, so a
            # request that already has one stops claiming the others.
            for case, claims in open_claims.items():
                have = {resource_type(r) for r in assigned[case]}
                for resource in [r for r in claims if resource_type(r) in have]:
                    del claims[resource]
                    changed = True
            claimants: dict[str, list[str]] = defaultdict(list)
            for case, claims in open_claims.items():
                for resource in claims:
                    claimants[resource].append(case)
            for resource, cases in claimants.items():
                if len(cases) == 1:
                    case = cases[0]
                    assigned[case][resource] = open_claims[case].pop(resource)
                    changed = True
            any_change = any_change or changed
        return any_change

    # -- one case -------------------------------------------------------------

    def deletion_intent(self, text: str) -> tuple[bool, str]:
        answer, reason = deletion_intent(text)
        if answer is None and self.gemini and self.gemini.ready:
            model_answer = self.gemini.deletion_intent(text)
            if model_answer is not None:
                return model_answer, "Gemini classified ambiguous language"
        return bool(answer), reason

    def category_of(self, resource: str, observations: list[Observation]) -> str:
        default = default_category(resource)
        stated = [o.category for o in observations if o.category and o.category != default]
        return stated[-1] if stated else default

    def required_action(self, category: str, observations: list[Observation], cutoff: str) -> str:
        rule = self.rules.get(category)
        if category == "DATA_FLOW":
            return "DISABLE"
        if not rule:
            return ""
        if category == "SECURITY_AUDIT_LOG":
            ends = [iso_at(o.fields.get("retention_until")) for o in observations if o.fields.get("retention_until")]
            if ends and max(ends) < cutoff:
                return "DELETE"   # outside the supplied retention period
        return rule["required_action"]

    def store_states(self, resource: str, observations: list[Observation],
                     cutoff: str, terms: set[str], category: str = "") -> dict[str, Observation]:
        """Latest observation of each store at the cutoff, including supported absence.

        A row missing from a snapshot proves absence only for account profiles:
        profile tables list accounts, while the other inventories are partial
        extracts (an export missing from July inventories is present again in a
        September log). A missing row never proves a retained record was lost.
        """
        omission_proves_absence = category == "USER_PROFILE"
        explicit: dict[str, Observation] = {}
        for obs in observations:
            current = explicit.get(obs.store)
            if current is None or obs.time() >= current.time():
                explicit[obs.store] = obs
        inferred: dict[str, Observation] = {}
        system = default_system(resource)
        for cover in self.evidence.coverage:
            if cover.system_id != system or cover.captured_at > cutoff or resource in cover.present:
                continue
            if not omission_proves_absence:
                break
            if cover.scoped and not (self.file_terms.get(cover.evidence_id, set()) & terms):
                continue
            current = inferred.get(cover.store)
            if current is None or cover.captured_at >= current.time():
                path = self.evidence_files.get(cover.evidence_id, {}).get("file_path", "")
                inferred[cover.store] = Observation(
                    evidence_id=cover.evidence_id, source_path=path,
                    location=f"absent from {cover.system_id} ({cover.store})", captured_at=cover.captured_at,
                    observed_at=cover.captured_at, system_id=system, resource_id=resource,
                    store=cover.store, category=default_category(resource), state="ABSENT",
                    identifying=None, method="coverage", inferred=True)
        chosen = dict(explicit)
        for store, obs in inferred.items():
            seen = chosen.get(store)
            if seen is None or (obs.time() > seen.time() and seen.state != "ABSENT"):
                chosen[store] = obs
        return chosen

    def investigate(self, request: dict[str, str]) -> tuple[dict[str, str], dict[str, Any]]:
        case = request["case_id"]
        cutoff = iso_at(request["audit_at"])
        authorized, intent_reason = self.deletion_intent(request["request_text"])
        if not authorized:
            return ({"case_id": case, "deletion_status": "NOT_APPLICABLE", "steps_json": "[]"},
                    {"intent": intent_reason, "resources": [], "uncertain": []})
        user, candidates = self.identity.resolve(request["email"], request["requested_at"])
        detail: dict[str, Any] = {"intent": intent_reason, "user_id": user, "identity_candidates": candidates,
                                  "resources": [], "uncertain": [], "unobserved": []}
        if not user:
            detail["uncertain"].append("request email does not identify exactly one person")
            return {"case_id": case, "deletion_status": "INCOMPLETE", "steps_json": "[]"}, detail
        terms = self.graph.identifier_terms(user)
        detail["historical_identifiers"] = self.identity.identity_evidence(user)
        linked = {node.split(":", 1)[1]: sorted({e.evidence_id for e in edges})
                  for node, edges in self.graph.links_to_person(user).items()}
        # A person can delete an account, sign up again and ask again. Records
        # whose whole evidence trail ended by an earlier request's audit belong
        # to that earlier deletion cycle, not to this one.
        earlier = [iso_at(r["audit_at"]) for r in self.cycles.get(user, [])
                   if iso_at(r["requested_at"]) < iso_at(request["requested_at"])]
        if earlier:
            boundary = max(earlier)
            for resource in [r for r in linked if self.last_seen.get(r, "") <= boundary]:
                detail.setdefault("earlier_cycle_resources", []).append(resource)
                del linked[resource]
        # A record first mentioned after the audit did not exist yet as far as
        # the evidence shows, so it is not part of this audit.
        for resource in [r for r in linked if self.first_seen.get(r, "") > cutoff]:
            detail.setdefault("later_resources", []).append(resource)
            del linked[resource]
        # This request's own workflow events date its resources, so they stay in scope.
        for resource, activity_id in self.assigned.get(case, {}).items():
            linked.setdefault(resource, []).append(f"workflow {activity_id}")
        detail["unassigned_workflow_resources"] = sorted(self.unresolved_claims.get(case, {}))
        findings: list[dict[str, Any]] = []
        remediation: list[dict[str, Any]] = []
        verification: list[dict[str, Any]] = []
        evidence_checks: list[dict[str, Any]] = []
        unknown = 0
        for resource in sorted(linked):
            if not RESOURCE.fullmatch(resource):
                continue
            available = [o for o in self.by_resource.get(resource, []) if o.available_at(cutoff)]
            category = self.category_of(resource, self.by_resource.get(resource, []))
            required = self.required_action(category, available, cutoff)
            target = policy_target(category, required)
            if not target:
                continue
            system = default_system(resource)
            finding_op, remedy_op, verify_op = STEP_FOR_TARGET[target]
            stores = self.store_states(resource, available, cutoff, terms, category)
            verdicts = {store: judge(obs, target) for store, obs in stores.items()}
            record = {"resource_id": resource, "system_id": system, "category": category,
                      "required": required, "target": target, "linked_by": linked[resource],
                      "stores": {s: {"state": o.state, "identifying": o.identifying, "evidence_id": o.evidence_id,
                                     "observed_at": o.time(), "verdict": verdicts[s], "inferred": o.inferred}
                                 for s, o in stores.items()}}
            detail["resources"].append(record)
            if not stores:
                detail["unobserved"].append(resource)
                verification.append(self._step("VERIFICATION", system, resource, verify_op, ""))
                continue
            bad = [stores[s] for s, v in verdicts.items() if v == "violation"]
            good = [stores[s] for s, v in verdicts.items() if v == "ok"]
            if bad and finding_op:
                proof = max(bad, key=lambda o: (o.time(), not o.inferred))
                findings.append(self._step("FINDING", system, resource, finding_op, proof.evidence_id))
                remediation.append(self._step("REMEDIATION", system, resource, remedy_op, ""))
                verification.append(self._step("VERIFICATION", system, resource, verify_op, ""))
            elif good and len(good) == len(stores):
                proof = max(good, key=lambda o: (o.time(), not o.inferred))
                evidence_checks.append(self._step("VERIFICATION", system, resource, verify_op, proof.evidence_id))
            else:
                unknown += 1
                detail["uncertain"].append(resource)
                verification.append(self._step("VERIFICATION", system, resource, verify_op, ""))
        if findings or unknown:
            status = "INCOMPLETE"
            for step in evidence_checks:
                step["evidence_id"] = ""
            verification.extend(evidence_checks)
        elif evidence_checks:
            # Every observed resource is compliant. Resources known only from
            # later evidence cannot be checked at audit_at, so they are reported
            # in the investigation detail instead of the verification list.
            status = "COMPLETE"
            verification = evidence_checks
        else:
            status = "INCOMPLETE"
            detail["uncertain"].append("no evidence before audit_at establishes any resource state")
        steps = order_steps(findings + remediation + verification)
        row = {"case_id": case, "deletion_status": status,
               "steps_json": json.dumps(steps, separators=(",", ":"), ensure_ascii=False)}
        return row, detail

    @staticmethod
    def _step(phase: str, system: str, resource: str, operation: str, evidence: str) -> dict[str, Any]:
        state_value = next(state for op, state in OPERATIONS[phase] if op == operation)
        return {"step_no": 0, "phase": phase, "system_id": system, "resource_id": resource,
                "operation": operation, "state": state_value, "evidence_id": evidence}
