"""Read participant evidence into observations that keep their provenance.

Every observation says: in this evidence file, at this time, this resource in
this store was seen in this state, and these identifiers were attached to it.
Nothing here decides which person a record belongs to or what policy needs;
that happens in the engine.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from typing import Any


RESOURCE = re.compile(
    r"\b(?:account|search|analytics|support|audit_record|backup|export|data_flow|tombstone|"
    r"cache|marketing|email_delivery|telemetry|legal_hold|object)_[0-9]+\b")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"\+\d[\d\s()\-]{6,}\d")

# Fields whose non-empty value is a direct identifier of a person.
DIRECT_FIELDS = {"email", "phone", "email_hash", "full_name", "name", "display_name",
                 "requester_email", "requester_phone", "requester_name", "customer_email",
                 "owner_email", "owner_phone"}
# Fields that point at a person through a stable internal reference.
REFERENCE_FIELDS = {"customer_reference", "subject_reference", "user_id", "subject_ref"}
# Context fields that carry the key a lookup was made with.
KEY_FIELDS = {"lookup_key", "match_key", "subject_key", "subject_ref", "owner_key", "scan_key",
              "correlation_key", "subject_reference", "matched_value", "subject_selector", "query"}
FREE_TEXT_FIELDS = {"message_text", "message_excerpt", "text"}
NAME_FIELDS = {"full_name", "name", "display_name", "requester_name"}

PREFIX_SYSTEM = [
    ("account_", "account_service"), ("search_", "search_service"),
    ("analytics_", "analytics_platform"), ("support_", "support_platform"),
    ("audit_record_", "security_service"), ("backup_", "backup_service"),
    ("export_", "export_service"), ("data_flow_", "integration_service"),
    ("tombstone_", "identity_service"), ("marketing_", "marketing_platform"),
    ("cache_", "cache_service"), ("email_delivery_", "email_delivery_service"),
    ("telemetry_", "analytics_platform"), ("legal_hold_", "legal_retention_service"),
    ("object_", "object_storage"),
]
PREFIX_CATEGORY = [
    ("account_", "USER_PROFILE"), ("search_", "SEARCH_INDEX_ENTRY"),
    ("analytics_", "ANALYTICS_PROFILE"), ("support_", "SUPPORT_MESSAGE"),
    ("audit_record_", "SECURITY_AUDIT_LOG"), ("backup_", "BACKUP_ARCHIVE"),
    ("export_", "USER_EXPORT"), ("data_flow_", "DATA_FLOW"),
    ("tombstone_", "ACCOUNT_TOMBSTONE"), ("marketing_", "MARKETING_PROFILE"),
    ("cache_", "CACHE_ENTRY"), ("email_delivery_", "EMAIL_DELIVERY_LOG"),
    ("telemetry_", "PRODUCT_TELEMETRY"), ("legal_hold_", "LEGAL_HOLD_RECORD"),
    ("object_", "SUPPORT_ATTACHMENT"),
]
# Table and collection names mapped to the service that owns them.
TABLE_SYSTEM = {
    "customer_profiles": "account_service", "customer_search_documents": "search_service",
    "search_documents": "search_service", "customer_analytics_profiles": "analytics_platform",
    "customer_analytics_reports": "analytics_platform", "support_records": "support_platform",
    "support_tickets": "support_platform", "customer_exports": "export_service",
    "customer_export_jobs": "export_service", "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service", "security_audit_records": "security_service",
    "security_audit_events": "security_service", "deleted_customer_references": "identity_service",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def iso_at(value: Any) -> str:
    """ISO UTC strings in this dataset sort chronologically after normalization."""
    if value is None:
        return ""
    return str(value).strip().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    """Normalize an identifier so differently formatted copies compare equal."""
    text = str(value or "").strip().lower()
    if not text or text in {"null", "none"}:
        return ""
    if "@" in text:
        return text
    digits = "".join(c for c in text if c.isdigit())
    if text.startswith("+") and len(digits) >= 7 and all(c.isdigit() or c in "+-() ." for c in text):
        return digits
    return text


def default_system(resource: str) -> str:
    for prefix, system in PREFIX_SYSTEM:
        if resource.startswith(prefix):
            return system
    return ""


def default_category(resource: str) -> str:
    for prefix, category in PREFIX_CATEGORY:
        if resource.startswith(prefix):
            return category
    return ""


def is_empty(value: Any) -> bool:
    return value is None or str(value).strip().lower() in {"", "null", "none"}


@dataclass
class Observation:
    """One supported statement about one resource in one store."""
    evidence_id: str
    source_path: str
    location: str
    captured_at: str
    observed_at: str
    system_id: str
    resource_id: str
    store: str
    category: str
    state: str                 # PRESENT, ABSENT, ACTIVE, DISABLED, RESTORABLE, BLOCKED, UNKNOWN
    identifying: bool | None   # for PRESENT records: are direct identifiers still visible?
    keys: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)
    fields: dict[str, Any] = field(default_factory=dict)
    method: str = ""
    inferred: bool = False     # absence inferred from a scoped snapshot, not stated outright

    def available_at(self, cutoff: str) -> bool:
        return self.captured_at <= cutoff and (not self.observed_at or self.observed_at <= cutoff)

    def time(self) -> str:
        return self.observed_at or self.captured_at


@dataclass
class Coverage:
    """A snapshot that lists every record of a store at one time."""
    evidence_id: str
    captured_at: str
    system_id: str
    store: str
    present: set[str]
    # Bundles only list records for the people they were exported for, so an
    # empty collection there proves absence only for a person the file covers.
    scoped: bool = False


@dataclass
class Document:
    evidence_id: str
    source_path: str
    captured_at: str
    method: str
    text: str
    errors: list[str]


@dataclass
class EvidenceIndex:
    root: Path
    observations: list[Observation] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    extraction_errors: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path) -> "EvidenceIndex":
        index = cls(root)
        for item in read_csv(root / "dataset" / "evidence_files.csv"):
            index._load_file(item)
        return index

    def _load_file(self, item: dict[str, str]) -> None:
        # The manifest path is relative to dataset/, not the repository root.
        relative = Path(item["file_path"])
        dataset = (self.root / "dataset").resolve()
        path = (dataset / relative).resolve()
        if not path.is_relative_to(dataset):
            self.extraction_errors.append({"file_id": item["file_id"], "error": "path escapes dataset"})
            return
        if not path.is_file():
            self.extraction_errors.append({"file_id": item["file_id"], "error": "file missing"})
            return
        try:
            payload = self._extract_cached(path)
        except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
            self.extraction_errors.append({"file_id": item["file_id"], "error": f"{type(exc).__name__}: {exc}"})
            return
        for error in payload.get("errors", []):
            self.extraction_errors.append({"file_id": item["file_id"], "error": error})
        captured = iso_at(item["captured_at"])
        source = str(relative)
        self.documents.append(Document(item["file_id"], source, captured, payload["method"],
                                       payload.get("text", ""), payload.get("errors", [])))
        for record in payload.get("records", []):
            self.observations.append(Observation(
                evidence_id=item["file_id"], source_path=source, location=record["location"],
                captured_at=captured, observed_at=iso_at(record.get("observed_at") or captured),
                system_id=record["system_id"], resource_id=record["resource_id"],
                store=record["store"], category=record["category"], state=record["state"],
                identifying=record["identifying"], keys=set(record["keys"]),
                references=set(record["references"]), fields=record["fields"],
                method=payload["method"]))
        for entry in payload.get("coverage", []):
            self.coverage.append(Coverage(item["file_id"], iso_at(entry.get("captured_at") or captured),
                                          entry["system_id"], entry["store"], set(entry["present"]),
                                          bool(entry.get("scoped"))))

    def _extract_cached(self, path: Path) -> dict[str, Any]:
        digest = hashlib.sha256(Path(__file__).read_bytes() + path.read_bytes()).hexdigest()
        cache_dir = self.root / ".cache" / "ghosttrace" / "extraction"
        cache_file = cache_dir / f"{digest}.json"
        if cache_file.is_file():
            return json.loads(cache_file.read_text(encoding="utf-8"))
        suffix = path.suffix.lower()
        if suffix == ".json":
            payload = json_payload(json.loads(path.read_text(encoding="utf-8")))
        elif suffix == ".sql":
            payload = sql_payload(path.read_text(encoding="utf-8"))
        elif suffix in {".log", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            payload = {"method": "text", "text": text, "records": text_records(text)}
        elif suffix == ".pdf":
            payload = pdf_payload(path)
        elif suffix in {".png", ".jpg", ".jpeg"}:
            payload = image_payload(path)
        else:
            payload = {"method": "unsupported", "records": [], "errors": [f"unsupported type {suffix}"]}
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload


# ---------------------------------------------------------------------------
# Turning one record (a row, a JSON object, a log line) into an observation.

def identifiers_in(fields: dict[str, Any]) -> tuple[set[str], set[str], bool]:
    """Return (direct identifiers, stable references, any direct identifier visible)."""
    direct: set[str] = set()
    references: set[str] = set()
    visible = False
    for key, value in fields.items():
        if is_empty(value) or isinstance(value, (dict, list)):
            continue
        if key in DIRECT_FIELDS:
            visible = True
            # A name shows the person but is too weak to link a record to them.
            if key not in NAME_FIELDS:
                direct.add(normalize(value))
        elif key in REFERENCE_FIELDS:
            references.add(normalize(value))
        elif key in FREE_TEXT_FIELDS:
            for email in EMAIL.findall(str(value)):
                direct.add(normalize(email))
                visible = True
            for phone in PHONE.findall(str(value)):
                direct.add(normalize(phone))
                visible = True
    direct.discard("")
    references.discard("")
    return direct, references, visible


def make_record(resource: str, location: str, fields: dict[str, Any], system: str = "",
                store: str = "primary", category: str = "", state: str = "",
                identifying: bool | None = None, keys: set[str] | None = None,
                observed_at: str = "") -> dict[str, Any]:
    clean = flatten(fields)
    category = category or default_category(resource)
    direct, references, visible = identifiers_in(clean)
    all_keys = {normalize(k) for k in (keys or set())} | direct
    all_keys.discard("")
    if not state:
        state = "PRESENT"
    if identifying is None and state == "PRESENT":
        identifying = visible
        if category in {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"} and references:
            # A stable user reference still makes analytics attributable.
            identifying = True
    return {"resource_id": resource, "location": location, "fields": clean,
            # Services name the same system in different ways; the resource
            # prefix gives the one canonical system ID used in the output.
            "system_id": default_system(resource) or system, "store": store,
            "category": category, "state": state,
            "identifying": identifying, "keys": sorted(all_keys | references),
            "references": sorted(references), "observed_at": observed_at}


def flatten(fields: dict[str, Any]) -> dict[str, Any]:
    """Scalar fields, including those of nested objects such as a search document.

    Lists are left out: they hold activity history, not identifiers of the record.
    """
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, dict):
            for inner_key, inner_value in flatten(value).items():
                clean.setdefault(inner_key, inner_value)
        elif not isinstance(value, list):
            clean[key] = value
    return clean


def record_state(resource: str, fields: dict[str, Any]) -> tuple[str, bool | None]:
    """State of a record that a structured source lists as existing."""
    if resource.startswith("data_flow_"):
        enabled = truth(fields.get("enabled"))
        if enabled is None and str(fields.get("last_delivery_status", "")).lower() == "stopped":
            enabled = False
        if enabled is None:
            return "UNKNOWN", None
        return ("ACTIVE" if enabled else "DISABLED"), None
    if resource.startswith("backup_"):
        allowed = truth(fields.get("restore_allowed"))
        if allowed is None:
            return "UNKNOWN", None
        return ("RESTORABLE" if allowed else "BLOCKED"), None
    if resource.startswith("export_"):
        available = truth(fields.get("download_available", fields.get("available")))
        if available is False:
            return "ABSENT", None
    status = str(fields.get("account_status", "")).upper()
    if resource.startswith("account_") and status == "DELETED":
        return "ABSENT", None
    return "PRESENT", None


def truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "enabled"}:
        return True
    if text in {"false", "0", "no", "disabled"}:
        return False
    return None


EXPLICIT_RESULTS = {
    "NOT_FOUND": ("ABSENT", None), "ABSENT": ("ABSENT", None), "DELETED": ("ABSENT", None),
    "NO_HITS": ("ABSENT", None), "MISS": ("ABSENT", None),
    "FOUND": ("PRESENT", None), "PRESENT": ("PRESENT", None), "HIT": ("PRESENT", None),
    "IDENTIFIABLE": ("PRESENT", True), "ANONYMIZED": ("PRESENT", False), "REDACTED": ("PRESENT", False),
    "ACTIVE": ("ACTIVE", None), "DISABLED": ("DISABLED", None), "BLOCKED": ("BLOCKED", None),
}


def explicit_state(resource: str, result: str) -> tuple[str, bool | None]:
    state, identifying = EXPLICIT_RESULTS.get(result.upper(), ("UNKNOWN", None))
    if state == "ACTIVE" and resource.startswith("backup_"):
        state = "RESTORABLE"
    return state, identifying


def keyed_identifying(state: str, identifying: bool | None, keys: set[str]) -> bool | None:
    """A FOUND row reported under a direct identifier still exposes that identifier."""
    if state != "PRESENT" or identifying is not None:
        return identifying
    if any("@" in k or (k.isdigit() and len(k) >= 7) or len(k) == 64 for k in map(normalize, keys)):
        return True
    return None


# ---------------------------------------------------------------------------
# JSON evidence.

def json_payload(obj: Any) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    if isinstance(obj, dict) and isinstance(obj.get("snapshots"), list):
        # A bundle can hold several partial snapshots of one collection, so only
        # an empty collection says that nothing is left for the people covered.
        for n, snap in enumerate(obj["snapshots"]):
            system = snap.get("data_store", "")
            collection = snap.get("collection", "")
            captured = snap.get("captured_at") or obj.get("exported_at")
            present = []
            for m, row in enumerate(snap.get("records", [])):
                resource = str(row.get("resource_id", ""))
                if not resource:
                    continue
                present.append(resource)
                state, identifying = record_state(resource, row)
                records.append(make_record(resource, f"$.snapshots[{n}].records[{m}]", row, system,
                                           category=collection_category(resource, collection),
                                           state=state, identifying=identifying, observed_at=captured))
            if not present and system:
                coverage.append({"system_id": system, "store": "primary", "present": [],
                                 "captured_at": captured, "scoped": True})
        return {"method": "json", "records": records, "coverage": coverage}
    if isinstance(obj, dict) and isinstance(obj.get("service_exports"), dict):
        for system, groups in obj["service_exports"].items():
            for group, rows in groups.items():
                for m, row in enumerate(rows):
                    records.extend(service_export_rows(system, f"$.service_exports.{system}.{group}[{m}]", row))
        return {"method": "json", "records": records}
    # Generic record lists (manifests, ledgers, report extracts, export jobs).
    rows = obj.get("records", []) if isinstance(obj, dict) else obj
    collection = obj.get("collection", "") if isinstance(obj, dict) else ""
    captured = obj.get("captured_at") or obj.get("exported_at") if isinstance(obj, dict) else ""
    for m, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict) or not row.get("resource_id"):
            continue
        resource = str(row["resource_id"])
        location = f"$.records[{m}]"
        observed = row.get("recorded_at") or row.get("observed_at") or captured
        if "result" in row:
            state, identifying = explicit_state(resource, str(row["result"]))
            keys = {str(row.get("subject_reference", ""))}
            identifying = keyed_identifying(state, identifying, keys)
            fields = {k: v for k, v in row.items() if k != "subject_reference"}
            records.append(make_record(resource, location, fields, row.get("system", ""),
                                       category=row.get("record_type", ""), state=state,
                                       identifying=identifying, keys=keys, observed_at=observed))
        else:
            state, identifying = record_state(resource, row)
            records.append(make_record(resource, location, row, row.get("system", ""),
                                       category=collection_category(resource, collection),
                                       state=state, identifying=identifying, observed_at=observed))
    return {"method": "json", "records": records}


def collection_category(resource: str, collection: str) -> str:
    if resource.startswith("analytics_") and "contact" in collection:
        return "MARKETING_PROFILE"
    return default_category(resource)


def service_export_rows(system: str, location: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    resource = str(row.get("resource_id", ""))
    observed = row.get("observed_at", "")
    if "hits" in row:
        hits = row["hits"] if isinstance(row["hits"], dict) else {}
        query = {str(row.get("query", ""))}
        out = []
        for d, doc in enumerate(hits.get("documents", []) or []):
            out.append(make_record(str(doc.get("resource_id", resource)), f"{location}.hits.documents[{d}]",
                                   doc, system, keys=query, observed_at=observed))
        if not out:
            out.append(make_record(resource, location, {}, system, state="ABSENT", keys=query,
                                   observed_at=observed))
        return out
    if "status_code" in row:
        if str(row["status_code"]) == "404" or not row.get("profile"):
            return [make_record(resource, location, {}, system, state="ABSENT", observed_at=observed)]
        return [make_record(resource, location, row["profile"], system, observed_at=observed)]
    state, identifying = record_state(resource, row)
    return [make_record(resource, location, row, system, state=state, identifying=identifying,
                        observed_at=observed)]


# ---------------------------------------------------------------------------
# SQL snapshots.

def sql_payload(script: str) -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    records: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    try:
        conn.executescript(script)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        scope: dict[str, str] = {}
        if "snapshot_scope" in tables:
            scope = {r[0]: r[1] for r in conn.execute("SELECT origin_system, captured_at FROM snapshot_scope")}
        stamp = TIMESTAMP.search(script[:600])
        file_time = stamp.group() if stamp else ""
        # store -> system -> present resource IDs
        seen: dict[tuple[str, str], list[str]] = {}
        for table in tables:
            if table == "snapshot_scope":
                continue
            store = table_store(table)
            quoted = '"' + table.replace('"', '""') + '"'
            for n, row in enumerate(conn.execute(f"SELECT * FROM {quoted}"), 1):
                data = dict(row)
                system = TABLE_SYSTEM.get(table_base(table), "")
                collection = ""
                if isinstance(data.get("record_json"), str):
                    system = data.get("origin_system") or system
                    collection = data.get("collection_name", "")
                    data = {**json.loads(data["record_json"]), "updated_at": data.get("updated_at")}
                resource = str(data.get("resource_id") or "")
                if not resource:
                    continue
                system = system or default_system(resource)
                seen.setdefault((store, system), []).append(resource)
                state, identifying = record_state(resource, data)
                records.append(make_record(resource, f"table:{table}:row:{n}", data, system, store=store,
                                           category=collection_category(resource, collection),
                                           state=state, identifying=identifying,
                                           observed_at=data.get("updated_at") or file_time))
        # Which (store, system) pairs this snapshot lists completely.
        stores = sorted({store for store, _ in seen} | ({"primary"} if scope else set()))
        systems = set(scope) | {system for _, system in seen}
        if any(table_base(t) == "customer_profiles" for t in tables):
            systems.add("account_service")
        for store in stores:
            for system in sorted(systems):
                if scope and system not in scope:
                    continue
                coverage.append({"system_id": system, "store": store,
                                 "present": seen.get((store, system), []),
                                 "captured_at": scope.get(system) or file_time})
        return {"method": "sqlite", "records": records, "coverage": coverage}
    finally:
        conn.close()


def table_base(table: str) -> str:
    for suffix in ("_primary", "_failover_replica", "_read_replica", "_reporting_replica"):
        if table.endswith(suffix):
            return table[: -len(suffix)]
    return table


def table_store(table: str) -> str:
    """Primary tables are the live service; every replica table is its own store."""
    if table.startswith("customer_data_"):
        name = table[len("customer_data_"):]
        return "primary" if name == "primary" else name
    for suffix in ("_failover_replica", "_read_replica", "_reporting_replica"):
        if table.endswith(suffix):
            return suffix[1:]
    return "primary"


# ---------------------------------------------------------------------------
# Text logs and line-oriented records.

KV = re.compile(r'([A-Za-z_][\w.]*)=(?:\\"((?:[^"\\]|\\[^"])*)\\"|"((?:[^"\\]|\\.)*)"|([^\s"]+))')


def parse_line(line: str) -> dict[str, str] | None:
    """Parse one log line of any of the supplied syntaxes into flat fields."""
    match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s*(.*)$", line)
    if not match:
        return None
    stamp, rest = match.groups()
    data: dict[str, str] = {}
    if rest.startswith("{"):
        try:
            data = {k: ("" if v is None else str(v)) for k, v in json.loads(rest).items()}
        except json.JSONDecodeError:
            data = {}
    if not data:
        if rest.startswith("|"):
            parts = [p.strip() for p in rest.strip("| ").split("|")]
            if len(parts) >= 4:
                data.update(level=parts[0], service=parts[1], trace_id=parts[2], event=parts[3])
                rest = " ".join(parts[4:])
        bracket = re.match(r"\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(\w+):\s*(.*)", rest)
        if bracket:
            data.update(level=bracket.group(1), service=bracket.group(2),
                        trace_id=bracket.group(3), event=bracket.group(4))
            rest = bracket.group(5)
        syslog = re.match(r"<\d+>\d+\s+\S+\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)", rest)
        if syslog:
            data.update(service=syslog.group(1), trace_id=syslog.group(2))
            rest = syslog.group(3)
        for key, escaped, quoted, bare in KV.findall(rest):
            value = escaped or quoted or bare
            if key == "message" and "=" in value:
                for k2, e2, q2, b2 in KV.findall(value.replace('\\"', '"')):
                    data.setdefault(k2, e2 or q2 or b2)
                continue
            data.setdefault(key, value)
        if "function" in data:
            data.setdefault("service", data["function"])
        if "requestId" in data:
            data.setdefault("trace_id", data["requestId"])
    data["timestamp"] = stamp
    return data


LOG_SYSTEMS = {"profile_service": "account_service", "support_replica": "support_platform"}
NOT_STATE_EVENTS = ("_started", "connection_acquired", "dependency_retry", "dependency_recovered",
                    "cache_lookup", "thumbnail_failed")


def text_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    trace_keys: dict[str, set[str]] = {}
    trace_source: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("- ") and stripped.count("|") >= 4:
            record = pipe_record(stripped[2:], f"line:{n}")
            if record:
                records.append(record)
            continue
        data = parse_line(stripped)
        if not data:
            continue
        trace = data.get("trace_id", "")
        keys = {data[k] for k in KEY_FIELDS if not is_empty(data.get(k))}
        if trace:
            trace_keys.setdefault(trace, set()).update(keys)
            keys = keys | trace_keys[trace]
            # The data source is named once, on the line that starts the trace.
            if data.get("source"):
                trace_source[trace] = data["source"]
            elif trace in trace_source:
                data["source"] = trace_source[trace]
        resource = data.get("resource_id", "")
        event = data.get("event", "")
        if not resource or not RESOURCE.fullmatch(resource) or event.endswith(NOT_STATE_EVENTS):
            continue
        record = log_record(resource, data, keys, f"line:{n}")
        if record:
            records.append(record)
    return records


def pipe_record(line: str, location: str) -> dict[str, Any] | None:
    """Handover rows: time | system | resource | record type | result | identifier."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 5 or not RESOURCE.fullmatch(parts[2]):
        return None
    stamp, system, resource, category, result = parts[:5]
    key = parts[5] if len(parts) > 5 else ""
    keys = set() if key.lower().startswith("no ") else {key}
    state, identifying = explicit_state(resource, result)
    identifying = keyed_identifying(state, identifying, keys)
    return make_record(resource, location, {"result": result}, system, category=category,
                       state=state, identifying=identifying, keys=keys, observed_at=stamp)


def log_record(resource: str, data: dict[str, str], keys: set[str], location: str) -> dict[str, Any] | None:
    service = data.get("service", "")
    system = LOG_SYSTEMS.get(service, service if service.endswith(("_service", "_platform")) else "")
    if not system or system in {"search_index_service", "downstream_delivery_service"}:
        system = data.get("system") or default_system(resource)
    event = data.get("event", "")
    fields = {k: v for k, v in data.items() if k not in KEY_FIELDS}
    category = data.get("record_type", "") or ""
    state, identifying = "UNKNOWN", None
    if "result" in data and data.get("record_type"):
        state, identifying = explicit_state(resource, data["result"])
        identifying = keyed_identifying(state, identifying, keys)
    elif data.get("http_status") == "404" or data.get("rows") == "0" or data.get("total_hits") == "0":
        state = "ABSENT"
    elif data.get("result", "").lower() in {"no_hits", "miss", "not_found"}:
        state = "ABSENT"
    elif event in {"message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"}:
        state, identifying = ("PRESENT", True) if not is_empty(data.get("matched_value")) else ("PRESENT", False)
        if event == "attachment_pii_scan":
            category = "SUPPORT_ATTACHMENT"
    elif "enabled" in data and resource.startswith("data_flow_"):
        state = "ACTIVE" if truth(data["enabled"]) else "DISABLED"
    elif resource.startswith("backup_") and ("restore_allowed" in data or "eligible" in data or "decision" in data):
        allowed = truth(data.get("restore_allowed"))
        if allowed is None and "eligible" in data:
            allowed = bool(truth(data["eligible"])) and not truth(data.get("deletion_block"))
        if allowed is None:
            allowed = str(data.get("decision", "")).lower() in {"allow", "allowed", "approved", "permit"}
        state = "RESTORABLE" if allowed else "BLOCKED"
    elif resource.startswith("export_") and "available" in data:
        state = "PRESENT" if truth(data["available"]) else "ABSENT"
    elif data.get("marker_found") == "false" or event == "deletion_marker_check":
        state = "PRESENT"
    elif data.get("rows", "").isdigit() and int(data["rows"]) > 0:
        state = "PRESENT"
    elif data.get("result", "").lower() in {"hit", "found"} or data.get("http_status") == "200":
        state = "PRESENT"
    elif event.endswith(("_completed", "_read", "_loaded")) or "direct_identifiers" in data:
        state = "PRESENT"
    if data.get("email_opt_in"):
        category = "MARKETING_PROFILE"
    if "contact_index" in data.get("source", ""):
        category = "SECONDARY_IDENTIFIER"   # a lookup index from contact details to the person
    if state == "PRESENT" and identifying is None:
        visible = any(not is_empty(fields.get(k)) for k in DIRECT_FIELDS)
        listed = str(data.get("direct_identifiers", "")).lower() not in {"", "none", "0", "false", "null"}
        if visible or listed:
            identifying = True
        elif any(k in fields for k in DIRECT_FIELDS):
            identifying = False
        elif resource.startswith(("analytics_", "telemetry_")) and not is_empty(data.get("customer_reference")):
            identifying = True
        else:
            identifying = keyed_identifying(state, None, keys)
    return make_record(resource, location, fields, system, category=category, state=state,
                       identifying=identifying, keys=keys, observed_at=data.get("timestamp", ""))


# ---------------------------------------------------------------------------
# Documents and screenshots: keep the text; resources are matched in the engine.

def pdf_payload(path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                              text=True, check=True, timeout=60)
        text = proc.stdout
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return {"method": "pdf_text", "text": "", "records": [], "errors": [str(exc)]}
    if text.strip():
        return {"method": "pdf_text", "text": text, "records": []}
    try:
        with tempfile.TemporaryDirectory(prefix="ghosttrace-pdf-") as temp:
            prefix = str(Path(temp) / "page")
            subprocess.run(["pdftoppm", "-f", "1", "-l", "20", "-r", "200", "-png", str(path), prefix],
                           capture_output=True, check=True, timeout=120)
            pages = sorted(Path(temp).glob("page-*.png"))
            text = "\n".join(subprocess.run(["tesseract", str(page), "stdout"], capture_output=True,
                                            text=True, check=True, timeout=90).stdout for page in pages)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return {"method": "pdf_ocr", "text": "", "records": [], "errors": [str(exc)]}
    return {"method": "pdf_ocr", "text": text, "records": [],
            "errors": [] if text.strip() else ["PDF OCR produced no text"]}


def image_payload(path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(["tesseract", str(path), "stdout"], capture_output=True,
                              text=True, check=True, timeout=90)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return {"method": "ocr", "text": "", "records": [], "errors": [str(exc)]}
    text = proc.stdout
    return {"method": "ocr", "text": text, "records": [],
            "errors": [] if text.strip() else ["OCR produced no text"]}
