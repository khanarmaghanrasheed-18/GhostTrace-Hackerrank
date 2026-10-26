"""Check whether a person's data still exists in any set of supplied files.

A check takes a subject (emails, phones, names, IDs) and a set of sources. It:
1. finds every record that contains one of the subject's identifiers;
2. follows supported links to further identifiers (a profile row's user ID, an
   identity-change row's old address), and only suggests weaker ones;
3. reconstructs the latest state of each stored copy at the chosen time;
4. applies the deletion policy to each copy and returns a verdict, the places
   the data still exists, a cleanup plan, a graph and a timeline.

Name-only matches are possible matches for a person to review, never proof.
Unreadable files are listed: nothing found in a file that could not be read
does not mean the data is gone.

The files are read once into an on-disk index (store.py), so a check looks the
person's identifiers up there instead of reading every record again.
"""

from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
import unicodedata

from . import whatif as benchmark_whatif
from .classify import (CATEGORY_WORDS, belongs_to_subject, category_for, column_kind, identifying_values,
                       load_policy, record_state)
from .engine import STEP_FOR_TARGET, deletion_intent, judge, order_steps, policy_target
from .ingest import EMAIL
from .readers import Record, Source, iso_time, SUPPORTED
from .store import (DOCUMENT_METHODS, HEX, OCR_METHODS, PHONE, Store, entity_key, indexed_token, page_of,
                    phone_key)
from .validate import OPERATIONS

OCR_LINK_CONFIDENCE = 0.8   # identifiers read by OCR below this are only suggested
MAX_POSSIBLE = 50           # possible matches listed one by one; the rest are counted
VERDICTS = {
    "STILL_EXISTS": "Personal data still exists",
    "NEEDS_REVIEW": "No confirmed remaining data, but some items need a person to review",
    "DELETED": "Deleted: every copy found has the treatment the policy requires",
    "NO_TRACE": "No trace found in the readable sources",
}


# ---------------------------------------------------------------------------
# The subject and its identifiers.

def norm_email(value: str) -> str:
    return value.strip().lower()


def norm_phone(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def norm_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def same_phone(a: str, b: str) -> bool:
    """Equal numbers, or the same last nine digits (national vs international format)."""
    return a == b or (len(a) >= 9 and len(b) >= 9 and a[-9:] == b[-9:])


def ocr_fold(text: str) -> str:
    """Undo the character confusions OCR makes most often."""
    return text.replace("rn", "m").replace("0", "o").replace("1", "l").replace("i", "l").replace("|", "l")


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


@dataclass
class Identifier:
    kind: str        # email, phone, id, hash or name
    value: str       # as written
    norm: str
    origin: str      # provided, linked or suggested
    how: str = ""
    evidence: str = ""   # where a linked or suggested identifier was found

    def key(self) -> str:
        return f"{self.kind}:{self.norm}"

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key(), "kind": self.kind, "value": self.value, "origin": self.origin,
                "how": self.how, "evidence": self.evidence}


def identifier(kind: str, value: str, origin: str, how: str = "", evidence: str = "") -> Identifier | None:
    value = str(value).strip()
    if not value:
        return None
    if HEX.fullmatch(value):
        kind = "hash"
    norm = {"email": norm_email, "phone": norm_phone, "name": norm_name, "hash": str.lower}.get(
        kind, lambda v: v.strip().lower())(value)
    if kind == "phone" and len(norm) < 7:
        return None
    if kind == "email" and "@" not in norm:
        return None
    if not norm or (kind in ("id", "name") and len(norm) < 3):
        return None
    return Identifier(kind, value, norm, origin, how, evidence)


class Subject:
    def __init__(self, emails=(), phones=(), names=(), ids=(), confirmed=(), rejected=()):
        self.items: dict[str, Identifier] = {}
        self.rejected = set(rejected)
        for kind, values in (("email", emails), ("phone", phones), ("name", names), ("id", ids)):
            for value in values:
                self.add(identifier(kind, value, "provided", "entered by the user"))
        for key in confirmed:
            kind, _, value = key.partition(":")
            self.add(identifier(kind, value, "provided", "confirmed by the user"))

    def add(self, item: Identifier | None) -> bool:
        if item is None or item.key() in self.rejected:
            return False
        current = self.items.get(item.key())
        if current is None or (current.origin == "suggested" and item.origin != "suggested"):
            self.items[item.key()] = item
            return True
        return False

    def active(self, kind: str) -> list[Identifier]:
        return [i for i in self.items.values() if i.kind == kind and i.origin != "suggested"]

    def hashes(self) -> dict[str, tuple[Identifier, str]]:
        """Hex digests a system may store instead of the plain email or phone."""
        found = {i.norm: (i, "stored hash") for i in self.active("hash")}
        for item in self.active("email") + self.active("phone"):
            for name in ("sha256", "sha1", "md5"):
                found[hashlib.new(name, item.norm.encode()).hexdigest()] = (item, f"{name} of {item.value}")
        return found

    def describe(self) -> list[dict[str, str]]:
        return [i.as_dict() for i in sorted(self.items.values(), key=lambda i: (i.origin != "provided", i.key()))]


@dataclass
class Match:
    identifier: str      # identifier key
    kind: str
    column: str
    strength: str        # strong or weak
    how: str
    value: str

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


def match_record(record: Record, subject: Subject, hashes: dict[str, tuple[Identifier, str]]) -> list[Match]:
    values = list(record.fields.items()) + ([("text", record.text)] if record.text else [])
    emails = {i.norm: i for i in subject.active("email")}
    phones = subject.active("phone")
    ids = subject.active("id")
    names = subject.active("name")
    found: dict[tuple[str, str], Match] = {}

    def keep(item: Identifier, column: str, strength: str, how: str, value: str) -> None:
        current = found.get((item.key(), column))
        if current is None or (current.strength == "weak" and strength == "strong"):
            found[(item.key(), column)] = Match(item.key(), item.kind, column, strength, how, value[:200])

    for column, value in values:
        text = str(value)
        lowered = text.lower()
        for email in EMAIL.findall(text):
            norm = norm_email(email)
            if norm in emails:
                keep(emails[norm], column, "strong", "exact email", email)
            elif record.method in OCR_METHODS:
                for known, item in emails.items():
                    if len(known) > 6 and edit_distance(ocr_fold(norm), ocr_fold(known)) <= 1:
                        keep(item, column, "weak", "one character differs (OCR can misread)", email)
        if phones:
            candidates = PHONE.findall(text) + ([text] if column_kind(column) == "phone" else [])
            for candidate in candidates:
                digits = norm_phone(candidate)
                for item in phones:
                    if len(digits) >= 7 and same_phone(digits, item.norm):
                        keep(item, column, "strong", "same phone number", candidate)
        for digest in HEX.findall(lowered):
            if digest in hashes:
                item, how = hashes[digest]
                keep(item, column, "strong", f"matches the {how}", digest)
        for item in ids:
            if lowered.strip() == item.norm or re.search(r"(?<![\w-])" + re.escape(item.norm) + r"(?![\w-])", lowered):
                keep(item, column, "strong", "exact ID", item.value)
        if names:
            plain = norm_name(text)
            for item in names:
                if re.search(r"(?<![a-z0-9])" + re.escape(item.norm) + r"(?![a-z0-9])", plain):
                    keep(item, column, "weak", "same name (names are not unique)", item.value)
    return list(found.values())


# ---------------------------------------------------------------------------
# Following links to more identifiers.

HISTORY_OLD = ("old_value", "previous_value", "before", "from_value", "old")
HISTORY_NEW = ("new_value", "current_value", "after", "to_value", "new")
PROFILE_KINDS = {"USER_PROFILE", "ACCOUNT_TOMBSTONE", "SECONDARY_IDENTIFIER"}
ANONYMIZE_KINDS = {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}
PLAIN_IDENTIFIER_COLUMNS = {"email", "e_mail", "email_address", "phone", "mobile", "phone_number", "user_id",
                            "customer_id", "email_hash", "uid", "member_id", "client_id", "account_id"}


def linked_identifiers(record: Record, category: str, finding_id: str) -> list[Identifier]:
    """Identifiers a strongly matched record ties to the same person.

    Linked (used automatically): the other values of an identity-change row, and
    the identifier columns of a profile row. Suggested (a person confirms): other
    subject-side identifier columns, e.g. customer_email on a ticket.
    """
    fields = {k.split(".")[-1].lower(): (k, v) for k, v in record.fields.items()}
    out: list[Identifier] = []
    old = next((fields[n] for n in HISTORY_OLD if n in fields), None)
    new = next((fields[n] for n in HISTORY_NEW if n in fields), None)
    changed = (fields.get("field_name") or fields.get("field") or fields.get("attribute") or ("", ""))[1].lower()
    if (old or new) and changed:
        kind = "phone" if "phone" in changed else "email" if "mail" in changed else "hash" if "hash" in changed else ""
        if kind:
            for pair in (old, new):
                if pair:
                    out.append(identifier(kind, pair[1], "linked", f"identity change row ({changed})", finding_id))
        for column, value in record.fields.items():
            if column_kind(column) == "id":
                out.append(identifier("id", value, "linked", "identity change row", finding_id))
        return [i for i in out if i]
    for column, value in record.fields.items():
        kind = column_kind(column)
        if kind == "name":
            if category in PROFILE_KINDS and (belongs_to_subject(column) or column.lower() in ("name", "full_name")):
                out.append(identifier("name", value, "linked", f"name on the profile ({column})", finding_id))
            continue
        if not kind:
            continue
        subject_side = belongs_to_subject(column) or column.split(".")[-1].lower() in PLAIN_IDENTIFIER_COLUMNS
        if category in PROFILE_KINDS and subject_side:
            out.append(identifier(kind, value, "linked", f"same profile record ({column})", finding_id))
        elif belongs_to_subject(column):
            out.append(identifier(kind, value, "suggested", f"{column} on a matched record", finding_id))
    out = [i for i in out if i]
    if record.method in OCR_METHODS and record.confidence < OCR_LINK_CONFIDENCE:
        # A misread character would point at someone else's data, so a person confirms it.
        for item in out:
            if item.origin == "linked":
                item.origin = "suggested"
                item.how += f" (read by OCR at {record.confidence:.0%} confidence; check it was read correctly)"
    return out


# ---------------------------------------------------------------------------
# Sources.

def collect_files(paths: list[Path]) -> list[Path]:
    """Every file under the given folders, skipping hidden files and folders (.cache, .git),
    which is also where GhostTrace keeps its own index."""
    files = []
    for path in paths:
        if path.is_dir():
            for found in sorted(path.rglob("*")):
                hidden = any(part.startswith(".") for part in found.relative_to(path).parts)
                if found.is_file() and not hidden:
                    files.append(found)
        elif path.is_file():
            files.append(path)
    return files


def read_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """A CSV naming each file (file_path, file or name) and when it was captured.

    Capture time matters for checks "as of" a date: without it the file's
    modified time is used, which is usually when it was copied, not captured.
    """
    meta: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            name = lowered.get("file_path") or lowered.get("file") or lowered.get("name") or ""
            when = lowered.get("captured_at") or lowered.get("captured") or lowered.get("exported_at") or ""
            if name:
                entry = {"captured_at": when}
                for extra in ("category", "store"):
                    if lowered.get(extra):
                        entry[extra] = lowered[extra]
                meta[Path(name).name] = entry
    return meta


def source_meta(meta: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    return meta.get(name) or meta.get(Path(name).name) or {}


def load_sources(paths: list[Path], cache_dir: Path | None, meta: dict[str, dict[str, Any]] | None = None,
                 base: Path | None = None, store: Store | None = None, prefix: str = "",
                 progress: Any = None) -> list[Source]:
    """Sources for every file, read into the index at cache_dir/index.sqlite (in memory without a cache_dir).
    Names are relative to base when given, and start with prefix."""
    if store is None:
        store = Store(cache_dir / "index.sqlite" if cache_dir else None)
    sources = []
    for path in collect_files(paths):
        name = prefix + (path.relative_to(base).as_posix() if base and path.is_relative_to(base) else path.name)
        stated = iso_time(source_meta(meta or {}, name).get("captured_at", ""))
        source = store.source(path, name, stated, progress)
        source.notes.insert(0, "capture time stated" if stated else
                            "capture time unknown; using the file's modified time")
        sources.append(source)
    return sources


# ---------------------------------------------------------------------------
# A check.

@dataclass
class Check:
    subject: Subject
    as_of: str = ""
    policy_file: Path | None = None
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)   # per-source: category, store, complete


def run_check(sources: list[Source], check: Check) -> dict[str, Any]:
    rules = load_policy(check.policy_file)
    subject = check.subject
    as_of = iso_time(check.as_of) if check.as_of else ""
    usable = [s for s in sources if not as_of or s.captured_at <= as_of]
    later_sources = [s.name for s in sources if s not in usable]
    # 1-2. Match, then follow links until no new identifier appears. Each identifier is
    # looked up in the index once; the records found so far are matched again each round.
    no_result = no_result_pages(usable)
    rounds = 0
    pool: dict[tuple[int, int], tuple[Source, Record]] = {}
    asked: set[str] = set()
    matches: dict[tuple[str, str], tuple[Record, Source, list[Match]]] = {}
    while True:
        rounds += 1
        hashes = subject.hashes()
        for position, record_id, source, record in candidates(usable, subject, hashes, asked):
            pool[(position, record_id)] = (source, record)
        matches = {}
        for key in sorted(pool):   # source order, then file order
            source, record = pool[key]
            if as_of and record.observed_at and record.observed_at > as_of:
                continue
            found = match_record(record, subject, hashes)
            if found:
                matches[(source.name, record.location)] = (record, source, found)
        shared = shared_identifiers(matches, check.meta)
        added = False
        for record, source, found in matches.values():
            strong = [m for m in found if m.strength == "strong"]
            if not strong or all(m.identifier in shared for m in strong):
                continue   # a value on several people's records says nothing about whose record this is
            place = page_of(record.location) if record.method in DOCUMENT_METHODS else record.location
            if record.method in OCR_METHODS and (source.name, place) in no_result:
                continue   # values typed into a search box are not stored data
            category, _ = category_for(record, source_meta(check.meta, source.name).get("category", ""))
            for item in linked_identifiers(record, category, f"{source.name} / {place}"):
                added = subject.add(item) or added
        if not added or rounds >= 6:
            break
    hashes = subject.hashes()
    for record, source, found in matches.values():
        strong = [m for m in found if m.strength == "strong"]
        if strong and all(m.identifier in shared for m in strong):
            for match in strong:
                match.strength = "weak"
                match.how += "; the same value is on records of other people"
    add_trace_matches(usable, matches)
    matches = group_documents(matches)
    # Name-only and other possible matches can run into thousands in a large customer
    # base; the first ones are listed and the rest counted.
    possible = [key for key, (_, _, found) in matches.items() if not any(m.strength == "strong" for m in found)]
    possible.sort(key=lambda key: all(m.kind == "name" for m in matches[key][2]))   # name-only ones last
    for key in possible[MAX_POSSIBLE:]:
        del matches[key]
    # 3. Findings, one per matched record, with the latest state per stored copy.
    findings = []
    for n, ((source_name, _), (record, source, found)) in enumerate(sorted(matches.items()), 1):
        meta = source_meta(check.meta, source_name)
        category, category_reason = category_for(record, meta.get("category", ""))
        state, state_reason = record_state(record, category)
        if record.method == "ocr" and state_reason == "record is stored":
            state_reason = "the screen shows this data, so the system behind the screen still holds it"
        strong = any(m.strength == "strong" for m in found)
        shown = identifying_values(record)
        identifying = bool(shown) or any(m.kind in ("email", "phone", "hash") and m.strength == "strong"
                                         for m in found)
        if category in ANONYMIZE_KINDS and any(m.kind == "id" and m.strength == "strong" for m in found):
            identifying = True   # a stable user reference still links analytics to the person
        if state == "PRESENT" and not identifying and category in ("USER_PROFILE", "PERSONAL_RECORD"):
            state_reason += ("; only an internal ID remains: if this is a kept deleted-account reference, "
                             "set the source's kind of data to ACCOUNT_TOMBSTONE")
        required = rules.get(category, rules.get("PERSONAL_RECORD", {})).get("required_action", "DELETE")
        rule_id = rules.get(category, {}).get("rule_id", "")
        if category == "DATA_FLOW":
            required = "DISABLE"   # a process is stopped, not deleted
            rule_id = rules.get("DATA_FLOW", {}).get("rule_id", "")
        retention_end = iso_time(record.fields.get("retention_until", ""))
        if category == "SECURITY_AUDIT_LOG" and retention_end and as_of and retention_end < as_of:
            required, rule_id = "DELETE", rules.get("PERSONAL_RECORD", {}).get("rule_id", "")
        target = policy_target(category, required)
        message = no_result.get((source_name, record.location), "") if record.method in OCR_METHODS else ""
        claim = message if message and only_searched_values(record, subject, hashes) else ""
        if claim:
            category, category_reason = "FORM_LOOKUP", "screenshot of a search that found nothing"
            state = "UNKNOWN"
            state_reason = (f"the screen shows \"{claim}\". That is the form's claim, not proof: a form shows only what "
                            "its query selects and can hide rows marked deleted. Add the database files or table "
                            "exports behind this form to confirm")
            identifying, shown, required, rule_id, target = False, [], "", "", ""
        observed = record.observed_at or source.captured_at
        findings.append({
            "id": f"F{n}", "source": source_name, "location": record.location, "container": record.container,
            "store": meta.get("store") or source_name, "entity": entity_key(record),
            "category": category, "category_reason": category_reason, "rule_id": rule_id, "policy": required,
            "required_state": target, "state": state, "state_reason": state_reason, "identifying": identifying,
            "identifying_values": [f"{c}={v}" for c, v in shown][:6], "strength": "strong" if strong else "weak",
            "matches": [m.as_dict() for m in found], "observed_at": observed, "captured_at": source.captured_at,
            "method": record.method, "confidence": round(record.confidence, 2),
            "excerpt": excerpt(record), "fields": dict(list(record.fields.items())[:40]), "claim": claim,
        })
    mark_superseded(findings, usable, check.meta)
    for finding in findings:
        decide(finding)
    result = assemble(sources, usable, later_sources, subject, findings, as_of, rules, rounds)
    unlisted = max(len(possible) - MAX_POSSIBLE, 0)
    result["counts"]["possible_not_listed"] = unlisted
    result["counts"]["needs_review"] += unlisted
    return result


def shared_identifiers(matches: dict, meta: dict[str, dict[str, Any]]) -> set[str]:
    """Phone numbers found on the profile rows of different people. Together those rows hold
    more than one email address, and no ID is on all of them: a household phone, a company
    switchboard or a placeholder number, which is not proof of who a record belongs to."""
    rows: dict[str, list[Record]] = defaultdict(list)
    for record, source, found in matches.values():
        category, _ = category_for(record, source_meta(meta, source.name).get("category", ""))
        if category in PROFILE_KINDS:
            for match in found:
                if match.strength == "strong" and match.kind == "phone":
                    rows[match.identifier].append(record)
    shared = set()
    for key, records in rows.items():
        if len(records) < 2:
            continue
        emails: set[str] = set()
        common: set[str] | None = None
        for record in records:
            ids = set()
            for column, value in record.fields.items():
                kind = column_kind(column)
                if kind == "email":
                    emails.update(norm_email(e) for e in EMAIL.findall(value))
                elif kind == "id":
                    ids.add(value.strip().lower())
            common = ids if common is None else common & ids
        if len(emails) > 1 and not common:
            shared.add(key)
    return shared


def in_files(sources: list[Source], found: list[tuple[str, int, Record]]) -> list[tuple[int, int, Source, Record]]:
    """Index results as (source position, record ID, source, record), in source order and
    then file order. Identical files share one index entry, so each gets its own copy."""
    by_digest: dict[str, list[tuple[int, Source]]] = defaultdict(list)
    for position, source in enumerate(sources):
        by_digest[source.digest].append((position, source))
    out = []
    for digest, record_id, record in found:
        for position, source in by_digest[digest]:
            out.append((position, record_id, source, replace(record, source=source.name)))
    out.sort(key=lambda item: (item[0], item[1]))
    return out


def candidates(sources: list[Source], subject: Subject, hashes: dict[str, tuple[Identifier, str]],
               asked: set[str]) -> list[tuple[int, int, Source, Record]]:
    """Records that may hold one of the subject's identifiers not looked up before (asked
    keeps those that were); match_record decides which of them match."""
    if not sources:
        return []
    keys, phrases = [], []

    def new(key: str) -> bool:
        if key in asked:
            return False
        asked.add(key)
        return True

    for item in subject.active("email"):
        for key in ("e:" + item.norm, "o:email"):   # o:email: OCR text with an email, for near-miss readings
            if new(key):
                keys.append(key)
    for item in subject.active("phone"):
        if new("p:" + phone_key(item.norm)):
            keys.append("p:" + phone_key(item.norm))
    for digest in hashes:
        if new("h:" + digest):
            keys.append("h:" + digest)
    for item in subject.active("id"):
        if indexed_token(item.norm):
            if new("t:" + item.norm):
                keys.append("t:" + item.norm)
        elif new("s:" + item.norm):
            phrases.append([item.norm])   # e.g. "AB/123", "jdoe" or "417": found by text search
    for item in subject.active("name"):
        if new("s:" + item.norm):
            phrases.append(item.norm.split())
    if not keys and not phrases:
        return []
    return in_files(sources, sources[0].store.find({s.digest for s in sources}, keys, phrases))


def add_trace_matches(sources: list[Source], matches: dict) -> None:
    """A log line about a resource shares the subject of its trace's lookup line."""
    done = set()
    for record, source, found in list(matches.values()):
        trace = record.fields.get("trace_id")
        if not trace or (source.name, trace) in done:
            continue
        done.add((source.name, trace))
        records = [replace(r, source=source.name) for r in source.store.trace(source.digest, trace)]
        hit = [r for r in records if (source.name, r.location) in matches]
        if not hit:
            continue
        first = matches[(source.name, hit[0].location)][2][0]
        for other in records:
            if (source.name, other.location) not in matches and other.fields.get("resource_id"):
                matches[(source.name, other.location)] = (other, source, [Match(
                    first.identifier, first.kind, "trace_id", "strong",
                    f"same trace {trace} as {hit[0].location}", trace)])


def no_result_pages(sources: list[Source]) -> dict[tuple[str, str], str]:
    """Screenshot pages that show a search finding nothing, with the message shown."""
    pages: dict[tuple[str, str], str] = {}
    if sources:
        found = sources[0].store.find({s.digest for s in sources}, ["o:no-result"])
        for _, _, source, record in in_files(sources, found):
            pages.setdefault((source.name, page_of(record.location)), record.text[:160])
    return pages


def only_searched_values(record: Record, subject: Subject, hashes: dict[str, tuple[Identifier, str]]) -> bool:
    """True when every identifier a page shows is one the person is already known by.

    On a search screen that found nothing, those values are what was typed into
    the search box. Any other identifier means the page shows stored data.
    """
    for column, value in record.fields.items():
        if column_kind(column) and not match_record(Record(record.source, "", fields={column: value}), subject, hashes):
            return False
    known = {i.norm for i in subject.active("email")}
    return all(norm_email(email) in known for email in EMAIL.findall(record.text))


def group_documents(matches: dict) -> dict:
    """One finding per document page instead of one per line of text."""
    grouped: dict[tuple[str, str], tuple[Record, Source, list[Match]]] = {}
    for key, (record, source, found) in matches.items():
        if record.method not in DOCUMENT_METHODS:
            grouped[key] = (record, source, found)
            continue
        place = page_of(record.location)
        gkey = (source.name, place)
        if gkey not in grouped:
            page = source.store.page(source.digest, place)
            page_text = [r.text for r in page if r.text]
            form = next((r.fields for r in page if r.location.endswith("form fields")), {})
            merged = Record(source.name, place, record.container, dict(form), text="\n".join(page_text)[:4000],
                            method=record.method, observed_at=record.observed_at,
                            confidence=min([r.confidence for r in page] or [record.confidence]))
            grouped[gkey] = (merged, source, [])
        for match in found:
            match.column = f"{match.column} ({record.location})" if match.column == "text" else match.column
            grouped[gkey][2].append(match)
    # A name-only line next to confirmed identifiers on the same page is part of that finding.
    for record, source, found in grouped.values():
        best: dict[str, Match] = {}
        for match in found:
            if match.identifier not in best or (best[match.identifier].strength == "weak" and match.strength == "strong"):
                best[match.identifier] = match
        found[:] = list(best.values())
    return grouped


def mark_superseded(findings: list[dict[str, Any]], sources: list[Source], meta: dict[str, dict[str, Any]]) -> None:
    """Keep only the latest observation of each copy; earlier ones stay as history.

    Copies are the same when they share a store label and entity key. A later
    source marked as a complete snapshot of that store that lacks the entity
    shows it was removed from the store.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        groups[(finding["store"], finding["entity"])].append(finding)
    complete = defaultdict(list)
    for source in sources:
        info = source_meta(meta, source.name)
        if info.get("complete"):
            complete[info.get("store") or source.name].append(source)
    for (store, entity), group in groups.items():
        group.sort(key=lambda f: (f["observed_at"], f["id"]))
        latest = group[-1]
        for older in group[:-1]:
            older["superseded_by"] = latest["id"]
        for snapshot in sorted(complete.get(store, []), key=lambda s: s.captured_at):
            if snapshot.captured_at <= latest["observed_at"] or latest["source"] == snapshot.name:
                continue
            present = snapshot.store.has_entity(snapshot.digest, entity)
            if not present and not entity.startswith(("row ", "line ", "sheet ", "table ", "page ")):
                latest["absent_in"] = {"source": snapshot.name, "captured_at": snapshot.captured_at}


def decide(finding: dict[str, Any]) -> None:
    if finding.get("superseded_by"):
        finding["verdict"] = "history"
        return
    if finding.get("claim"):
        finding["verdict"] = "claim"
        return
    if finding["strength"] == "weak":
        finding["verdict"] = "possible match"
        return
    state = "ABSENT" if finding.get("absent_in") else finding["state"]
    observation = SimpleNamespace(state=state, identifying=finding["identifying"])
    finding["verdict"] = {"ok": "compliant", "violation": "violation", "unknown": "unknown"}[
        judge(observation, finding["required_state"])]


def excerpt(record: Record) -> str:
    if record.text:
        return record.text[:240]
    return "; ".join(f"{k}={v}" for k, v in list(record.fields.items())[:8])[:240]


# ---------------------------------------------------------------------------
# Verdict, plan, recreation paths, graph and timeline.

def assemble(sources: list[Source], usable: list[Source], later: list[str], subject: Subject,
             findings: list[dict[str, Any]], as_of: str, rules: dict[str, dict[str, str]], rounds: int) -> dict[str, Any]:
    current = [f for f in findings if f["verdict"] != "history"]
    violations = [f for f in current if f["verdict"] == "violation"]
    unknown = [f for f in current if f["verdict"] == "unknown"]
    possible = [f for f in current if f["verdict"] == "possible match"]
    compliant = [f for f in current if f["verdict"] == "compliant"]
    claims = [f for f in current if f["verdict"] == "claim"]
    unbacked = claims if not compliant else []   # a form's "not found" with no stored evidence behind it
    unreadable = [s.name for s in usable if s.errors and not s.count]
    paths = recreation_paths(current)
    if violations or paths:
        status = "STILL_EXISTS"
    elif unknown or possible or unbacked or unreadable:
        status = "NEEDS_REVIEW"
    elif compliant:
        status = "DELETED"
    else:
        status = "NO_TRACE"
    plan = cleanup_plan(current, status)
    result = {
        "label": "Result limited to the supplied sources; nothing outside them was checked.",
        "status": status, "status_text": VERDICTS[status], "as_of": as_of,
        "subject": subject.describe(),
        "suggested_identifiers": [i.as_dict() for i in subject.items.values() if i.origin == "suggested"],
        "counts": {"sources": len(sources), "sources_used": len(usable), "records_scanned": sum(s.count for s in usable),
                   "findings": len(current), "still_exists": len(violations), "compliant": len(compliant),
                   "needs_review": len(unknown) + len(possible) + len(unbacked), "claims": len(claims),
                   "history": len(findings) - len(current),
                   "identity_rounds": rounds},
        "findings": findings, "recreation_paths": paths, "plan": plan,
        "coverage": {"sources": [s.summary() for s in sources], "unreadable": unreadable,
                     "after_as_of": later},
        "policy": [rules[k] for k in sorted(rules)],
    }
    result["graph"] = build_graph(result)
    result["timeline"] = build_timeline(result, sources)
    return result


DESTINATION_FIELDS = ("destination", "target", "sink", "destination_system", "target_system", "connector_name",
                      "connector", "name")


def flow_destination_category(finding: dict[str, Any]) -> tuple[str, str]:
    lowered = {k.lower().split(".")[-1]: v for k, v in finding["fields"].items()}
    texts = [lowered[n] for n in DESTINATION_FIELDS if lowered.get(n)] or [finding["excerpt"]]
    for text in texts:
        place = re.sub(r"[\s\-.]+", "_", text.lower())
        for category, words in CATEGORY_WORDS:
            if category not in ("DATA_FLOW", "BACKUP_ARCHIVE", "USER_PROFILE") and re.search(words, place):
                return category, f"from '{text}'"
    return "", "destination not stated in the source"


def recreation_paths(current: list[dict[str, Any]], overrides: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Open sync or restore processes tied to the person, and what they could bring back."""
    paths = []
    records = [f for f in current if f["strength"] == "strong" and f["required_state"] in ("ABSENT", "REDACTED", "ANONYMIZED")]
    for finding in current:
        if finding["strength"] != "strong" or finding["category"] not in ("DATA_FLOW", "BACKUP_ARCHIVE"):
            continue
        state = (overrides or {}).get(finding["id"], "ABSENT" if finding.get("absent_in") else finding["state"])
        if state not in ("ACTIVE", "RESTORABLE"):
            continue
        if finding["category"] == "DATA_FLOW":
            category, basis = flow_destination_category(finding)
            affected = [f["id"] for f in records if category and f["category"] == category]
            kind = "sync"
        else:
            category, basis = "", "archive contents are not itemized; any archived database or file record can return"
            affected = [f["id"] for f in records if f["method"] not in DOCUMENT_METHODS]
            kind = "restore"
        paths.append({"process": finding["id"], "kind": kind, "state": state, "where": f"{finding['source']} / {finding['location']}",
                      "destination_category": category, "basis": basis, "can_recreate": affected,
                      "state_reason": finding["state_reason"]})
    return paths


def cleanup_plan(current: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    steps = []
    for finding in current:
        if finding["verdict"] not in ("violation", "compliant", "unknown"):
            continue
        finding_op, remedy_op, verify_op = STEP_FOR_TARGET[finding["required_state"]]
        where = f"{finding['source']} / {finding['location']}"
        base = {"system_id": finding["store"], "resource_id": finding["id"], "where": where,
                "category": finding["category"], "rule_id": finding["rule_id"]}
        if finding["verdict"] == "violation" and finding_op:
            steps.append({**base, "phase": "FINDING", "operation": finding_op, "evidence_id": finding["id"]})
            steps.append({**base, "phase": "REMEDIATION", "operation": remedy_op, "evidence_id": ""})
            steps.append({**base, "phase": "VERIFICATION", "operation": verify_op, "evidence_id": ""})
        elif finding["verdict"] == "compliant":
            steps.append({**base, "phase": "VERIFICATION", "operation": verify_op,
                          "evidence_id": finding["id"] if status in ("DELETED", "NO_TRACE") else ""})
        else:
            steps.append({**base, "phase": "VERIFICATION", "operation": verify_op, "evidence_id": ""})
    for step in steps:
        step["state"] = dict(OPERATIONS[step["phase"]])[step["operation"]]
        step["step_no"] = 0
    return order_steps(steps)


def build_graph(result: dict[str, Any]) -> dict[str, Any]:
    """Nodes and edges for the graph explorer. Every edge says how it is known."""
    nodes: dict[str, dict[str, Any]] = {"person": {"id": "person", "type": "person", "label": "Subject"}}
    edges: list[dict[str, Any]] = []
    for item in result["subject"] + result["suggested_identifiers"]:
        node = f"id:{item['key']}"
        if node in nodes:
            continue
        nodes[node] = {"id": node, "type": "identifier", "label": item["value"], "kind": item["kind"],
                       "origin": item["origin"], "how": item["how"], "evidence": item["evidence"]}
        kind = {"provided": "provided", "linked": "derived", "suggested": "suggested"}[item["origin"]]
        edges.append({"source": "person", "target": node, "relation": item["origin"].upper(), "kind": kind,
                      "evidence": item["evidence"] or "entered by the user", "note": item["how"]})
    for finding in result["findings"]:
        node = f"rec:{finding['id']}"
        node_type = "process" if finding["category"] in ("DATA_FLOW", "BACKUP_ARCHIVE") else "record"
        nodes[node] = {"id": node, "type": node_type, "label": f"{finding['id']} {finding['category']}",
                       "verdict": finding["verdict"], "finding": finding["id"]}
        src = f"src:{finding['source']}"
        nodes.setdefault(src, {"id": src, "type": "source", "label": finding["source"]})
        edges.append({"source": node, "target": src, "relation": "STORED_IN", "kind": "observed",
                      "evidence": finding["location"], "time": finding["observed_at"], "note": finding["container"]})
        for match in finding["matches"]:
            target = f"id:{match['identifier']}"
            if target not in nodes:
                continue
            relation, kind, note = "APPEARS_IN", "observed", match["how"]
            if finding["verdict"] == "claim":
                relation, kind, note = "SEARCHED_FOR", "claimed", f"typed into a search that reports: {finding['claim']}"
            elif match["strength"] != "strong":
                relation, kind = "POSSIBLE_MATCH", "weak"
            edges.append({"source": target, "target": node, "relation": relation, "kind": kind,
                          "evidence": f"{finding['source']} / {finding['location']} ({match['column']})",
                          "time": finding["observed_at"], "note": note})
        if finding.get("superseded_by"):
            edges.append({"source": f"rec:{finding['superseded_by']}", "target": node, "relation": "SUPERSEDES",
                          "kind": "observed", "evidence": finding["superseded_by"], "time": finding["observed_at"],
                          "note": "later observation of the same stored copy"})
    for item in result["subject"]:
        if item["origin"] == "linked" and item["evidence"]:
            source = next((f for f in result["findings"]
                           if f"{f['source']} / {f['location']}" == item["evidence"]), None)
            if source:
                edges.append({"source": f"rec:{source['id']}", "target": f"id:{item['key']}", "relation": "REVEALS",
                              "kind": "derived", "evidence": item["evidence"], "note": item["how"]})
    for path in result["recreation_paths"]:
        for target in path["can_recreate"]:
            edges.append({"source": f"rec:{path['process']}", "target": f"rec:{target}", "relation": "CAN_RECREATE",
                          "kind": "derived", "evidence": path["where"], "note": path["basis"]})
    return {"nodes": list(nodes.values()), "edges": edges}


def build_timeline(result: dict[str, Any], sources: list[Source]) -> list[dict[str, str]]:
    entries = []
    involved = {f["source"] for f in result["findings"]}
    for source in sources:
        if source.name in involved:
            entries.append({"time": source.captured_at, "type": "source captured", "source": source.name,
                            "what": f"{source.name} captured ({source.count} records)", "captured_at": ""})
    for finding in result["findings"]:
        what = f"{finding['id']} {finding['category']}: {finding['state']} ({finding['state_reason']})"
        if finding.get("superseded_by"):
            what += f"; later observation {finding['superseded_by']}"
        if finding.get("absent_in"):
            what += f"; missing from complete snapshot {finding['absent_in']['source']}"
        fields = {k.lower(): v for k, v in finding["fields"].items()}
        if fields.get("old_value") or fields.get("new_value"):
            entries.append({"time": finding["observed_at"], "type": "recorded event", "source": finding["id"],
                            "what": f"{fields.get('field_name', 'identifier')} changed from {fields.get('old_value', '')} "
                                    f"to {fields.get('new_value', '')}", "captured_at": finding["captured_at"]})
        entries.append({"time": finding["observed_at"], "type": "observed state", "source": finding["id"],
                        "what": what, "captured_at": finding["captured_at"]})
    if result["as_of"]:
        entries.append({"time": result["as_of"], "type": "check time", "source": "",
                        "what": "state is judged here; later records are not used", "captured_at": ""})
    entries.sort(key=lambda e: (e["time"] or "9999", e["type"], e["source"]))
    return entries


# ---------------------------------------------------------------------------
# What-if analysis on a finished check.

NOUN_CATEGORIES = [
    (r"\b(?:accounts?|profiles?|customers?|users?)\b", ["USER_PROFILE"]),
    (r"\bsearch\w*|\bindex\w*", ["SEARCH_INDEX_ENTRY"]),
    (r"\banalytics\b|\btracking\b", ["ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"]),
    (r"\b(?:support|tickets?|messages?|conversations?)\b", ["SUPPORT_MESSAGE"]),
    (r"\battachments?\b|\buploads?\b", ["SUPPORT_ATTACHMENT"]),
    (r"\b(?:exports?|downloads?)\b", ["USER_EXPORT"]),
    (r"\b(?:flows?|connectors?|integrations?|sync\w*|pipelines?|webhooks?)\b", ["DATA_FLOW"]),
    (r"\b(?:backups?|archives?|restores?)\b", ["BACKUP_ARCHIVE"]),
    (r"\b(?:audit|security)\b", ["SECURITY_AUDIT_LOG"]),
    (r"\b(?:invoices?|billing|payments?|orders?|tax)\b", ["FINANCIAL_RECORD"]),
    (r"\bmarketing\b|\bcampaigns?\b|\bnewsletters?\b|\bcrm\b", ["MARKETING_PROFILE"]),
    (r"\btombstones?\b", ["ACCOUNT_TOMBSTONE"]),
    (r"\bcaches?\b", ["CACHE_ENTRY"]),
    (r"\bemail logs?\b|\bdelivery logs?\b", ["EMAIL_DELIVERY_LOG"]),
]


def current_findings(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in result["findings"] if f["verdict"] not in ("history", "possible match", "claim")}


def validate_action(result: dict[str, Any], action: str, target: str | None) -> dict[str, Any]:
    action = (action or "").upper()
    findings = current_findings(result)
    if action not in benchmark_whatif.ACTION_RESULT:
        return {"status": "rejected", "action": action, "target": target,
                "reason": f"unsupported action; allowed: {', '.join(benchmark_whatif.ACTIONS)}"}
    if not target:
        return {"status": "clarify", "action": action, "target": None, "candidates": sorted(findings),
                "reason": "the proposal does not say which item"}
    if target not in findings:
        known = any(f["id"] == target for f in result["findings"])
        return {"status": "rejected", "action": action, "target": target,
                "reason": "only earlier history or a possible match, not a confirmed current copy" if known
                else "no such item in this check"}
    category = findings[target]["category"]
    if action == "DISABLE_DATA_FLOW" and category != "DATA_FLOW":
        return {"status": "rejected", "action": action, "target": target, "reason": "only a data flow can be disabled"}
    if action == "BLOCK_RESTORE" and category != "BACKUP_ARCHIVE":
        return {"status": "rejected", "action": action, "target": target, "reason": "only an archive can have restore blocked"}
    if category == "DATA_FLOW" and action != "DISABLE_DATA_FLOW":
        return {"status": "rejected", "action": action, "target": target, "reason": "a data flow is disabled, not edited"}
    if category == "BACKUP_ARCHIVE" and action in ("REDACT_RECORD", "ANONYMIZE_RECORD"):
        return {"status": "rejected", "action": action, "target": target,
                "reason": "an archive cannot be redacted in place; block restore instead"}
    return {"status": "accepted", "action": action, "target": target}


def interpret_question(result: dict[str, Any], question: str, gemini: Any = None) -> dict[str, Any]:
    """Translate a question into checked proposals. Gemini is used only when allowed."""
    findings = current_findings(result)
    proposals: list[dict[str, Any]] = []
    interpreter = "local-rules"
    if gemini is not None and gemini.ready:
        listing = [{"resource_id": f["id"], "system_id": f["source"], "category": f["category"]}
                   for f in findings.values()]
        answer = gemini.propose_actions(question, listing, benchmark_whatif.ACTIONS)
        if isinstance(answer, dict) and isinstance(answer.get("actions"), list):
            interpreter = f"gemini:{gemini.model}"
            proposals = [{"action": a.get("action"), "target": a.get("resource_id"),
                          "candidates": a.get("candidates") or []} for a in answer["actions"] if isinstance(a, dict)]
    if interpreter == "local-rules":
        proposals = local_rules(result, question)
    out: dict[str, Any] = {"interpreter": interpreter, "accepted": [], "clarify": [], "rejected": []}
    for item in proposals:
        if item.get("missing"):
            out["rejected"].append({"status": "rejected", "action": item["action"], "target": None,
                                    "reason": item["missing"]})
            continue
        candidates = [c for c in item.get("candidates", []) if c in findings]
        target = item.get("target") or (candidates[0] if len(candidates) == 1 else None)
        check = validate_action(result, item.get("action") or "", target)
        if check["status"] == "clarify":
            check["candidates"] = candidates or check["candidates"]
            check["question"] = f"Which item do you mean for {check['action']}? Options: {', '.join(check['candidates'])}"
        out[check["status"]].append(check)
    if not proposals:
        out["clarify"].append({"status": "clarify", "action": None, "target": None, "candidates": [],
                               "question": "No supported action was recognized. Try 'disable the connector and "
                                           "delete the search entries' or 'apply the plan'."})
    return out


def local_rules(result: dict[str, Any], question: str) -> list[dict[str, Any]]:
    findings = current_findings(result)
    if re.search(r"\b(?:apply|follow|do) (?:the )?(?:whole |cleanup )?plan\b|\beverything\b|\bfix (?:it )?all\b",
                 question, re.I):
        plan = [s for s in result["plan"] if s["phase"] == "REMEDIATION"]
        return [{"action": s["operation"], "target": s["resource_id"]} for s in plan]
    proposals = []
    action = ""
    for clause in re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b|\balso\b|\bplus\b)\s*", question, flags=re.I):
        for pattern, verb in benchmark_whatif.VERBS:
            if re.search(pattern, clause, re.I):
                action = verb
                break
        if not action or not clause.strip():
            continue
        ids = re.findall(r"\bF\d+\b", clause, re.I)
        if ids:
            proposals.extend({"action": action, "target": i.upper()} for i in ids)
            continue
        named = [f for f in findings.values() if Path(f["source"]).stem.lower() in clause.lower()]
        groups = [(pattern, cats) for pattern, cats in NOUN_CATEGORIES if re.search(pattern, clause, re.I)]
        if action == "BLOCK_RESTORE":
            groups = [(r"\b(?:backups?|archives?|restores?)\b", ["BACKUP_ARCHIVE"])]
        if named:
            proposals.extend({"action": action, "target": f["id"]} for f in named)
            continue
        if not groups:
            options = [i for i, f in findings.items() if validate_action(result, action, i)["status"] == "accepted"]
            proposals.append({"action": action, "target": options[0] if len(options) == 1 else None,
                              "candidates": options})
            continue
        for pattern, cats in groups:
            options = [i for i, f in findings.items() if f["category"] in cats]
            words = [w.lower() for w in re.findall(pattern, clause, re.I)]
            plural = any(w.endswith("s") and not w.endswith(("analytics", "ss")) for w in words)
            if not options:
                proposals.append({"action": action, "missing": f"this check found no {cats[0].lower()} item"})
            elif len(options) == 1 or plural:
                proposals.extend({"action": action, "target": o} for o in options)
            else:
                proposals.append({"action": action, "target": None, "candidates": options})
    return proposals


def whatif_assess(result: dict[str, Any], actions: list[dict[str, str]]) -> dict[str, Any]:
    findings = current_findings(result)
    applied = {a["target"]: benchmark_whatif.ACTION_RESULT[a["action"]] for a in actions}
    outcome: dict[str, list[dict[str, str]]] = {"resolved": [], "still required": [], "policy conflict": [],
                                                 "compliant already": [], "needs review": []}
    for fid, finding in sorted(findings.items(), key=lambda kv: int(kv[0][1:])):
        entry = {"id": fid, "where": f"{finding['source']} / {finding['location']}", "category": finding["category"],
                 "rule_id": finding["rule_id"], "policy": finding["policy"], "required": finding["required_state"]}
        if fid in applied:
            status, reason = benchmark_whatif.treatment_check(finding["required_state"], applied[fid])
            outcome[status].append({**entry, "reason": reason})
        elif finding["verdict"] == "violation":
            outcome["still required"].append({**entry, "reason": "still non-compliant; no proposed action treats it"})
        elif finding["verdict"] == "unknown":
            outcome["needs review"].append({**entry, "reason": "its state is not established"})
        else:
            outcome["compliant already"].append({**entry, "reason": "already compliant"})
    overrides = {fid: state for fid, state in applied.items() if findings[fid]["category"] in ("DATA_FLOW", "BACKUP_ARCHIVE")}
    paths = recreation_paths(list(findings.values()), overrides)
    changed = [fid for fid in applied if fid not in overrides]
    for path in paths:
        path["would_recreate_changed"] = sorted(set(path["can_recreate"]) & set(changed))
    warnings = []
    open_paths = {p["process"]: p for p in recreation_paths(list(findings.values()))}
    for item in actions:
        if item["target"] in open_paths:
            open_paths.pop(item["target"])
            continue
        for process, path in open_paths.items():
            if item["target"] in path["can_recreate"]:
                warnings.append(f"{item['target']} is changed before {process} is closed; close the {path['kind']} "
                                "path first or the change can be undone")
    done = not (outcome["still required"] or outcome["policy conflict"] or outcome["needs review"] or paths)
    return {"label": "HYPOTHETICAL - limited to the supplied sources. Nothing was changed anywhere.",
            "actions": actions, "outcome": outcome, "recreation_paths": paths, "order_warnings": warnings,
            "would_be": "DELETED" if done else "STILL_EXISTS" if outcome["still required"] or paths else "NEEDS_REVIEW"}


def explain_whatif(assessment: dict[str, Any]) -> str:
    lines = [assessment["label"], "Proposed: " + (", ".join(f"{a['action']} {a['target']}" for a in assessment["actions"])
                                                  or "nothing yet"),
             f"If carried out: {VERDICTS[assessment['would_be']]}."]
    for key in ("resolved", "policy conflict", "still required", "needs review"):
        for entry in assessment["outcome"][key]:
            lines.append(f"- {key.capitalize()}: {entry['id']} {entry['where']} [{entry['rule_id']} {entry['category']}]: "
                         f"{entry['reason']}")
    for path in assessment["recreation_paths"]:
        line = f"- Recreation risk: {path['process']} ({path['where']}) is {path['state']}; {path['basis']}"
        if path["would_recreate_changed"]:
            line += f"; it could bring back {', '.join(path['would_recreate_changed'])}"
        lines.append(line)
    lines.extend(f"- Order: {w}" for w in assessment["order_warnings"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batch checks from a request list.

REQUEST_COLUMNS = {"email": ("email", "emails", "e-mail", "email_address"), "phone": ("phone", "phones", "mobile"),
                   "name": ("name", "full_name", "names"), "id": ("user_id", "customer_id", "id", "ids", "account_id"),
                   "as_of": ("as_of", "audit_at", "check_at"), "label": ("request_id", "case_id", "reference"),
                   "text": ("request_text", "message", "request")}


def batch(sources: list[Source], rows: list[dict[str, str]], policy_file: Path | None = None,
          meta: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out = []
    for n, row in enumerate(rows, 1):
        lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        pick = {key: next((lowered[c] for c in names if lowered.get(c)), "") for key, names in REQUEST_COLUMNS.items()}
        label = pick["label"] or f"row {n}"
        intent = True
        if pick["text"]:
            intent, reason = deletion_intent(pick["text"])
            if intent is False:
                out.append({"request": label, "status": "NOT_APPLICABLE", "status_text": f"not a deletion request: {reason}",
                            "still_exists": 0, "needs_review": 0, "where": ""})
                continue
        split = lambda value: [v.strip() for v in re.split(r"[;|]", value) if v.strip()]
        subject = Subject(split(pick["email"]), split(pick["phone"]), split(pick["name"]), split(pick["id"]))
        if not subject.items:
            out.append({"request": label, "status": "NEEDS_REVIEW", "status_text": "no identifier in this row",
                        "still_exists": 0, "needs_review": 0, "where": ""})
            continue
        result = run_check(sources, Check(subject, pick["as_of"], policy_file, meta or {}))
        where = "; ".join(f"{f['id']} {f['source']} / {f['location']}" for f in result["findings"]
                          if f["verdict"] == "violation")
        if intent is None:
            # Unclear wording (e.g. "do you still hold my data?") is for a person to read.
            out.append({"request": label, "status": "NEEDS_REVIEW",
                        "status_text": "the request does not clearly ask for deletion; what was found is listed for review",
                        "still_exists": result["counts"]["still_exists"], "needs_review": result["counts"]["needs_review"],
                        "where": where})
            continue
        out.append({"request": label, "status": result["status"], "status_text": result["status_text"],
                    "still_exists": result["counts"]["still_exists"], "needs_review": result["counts"]["needs_review"],
                    "where": where})
    return out


def supported_types() -> list[str]:
    return sorted(SUPPORTED)


def to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False)
