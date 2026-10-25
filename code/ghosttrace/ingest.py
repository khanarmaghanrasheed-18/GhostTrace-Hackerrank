"""Read participant evidence while retaining provenance and extraction errors."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
from typing import Any


RESOURCE = re.compile(r"\b(?:account|search|analytics|support|audit_record|backup|export|data_flow|tombstone|profile|cache|marketing|attachment|email_log|telemetry|legal_hold)_[0-9]+\b", re.I)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def iso_at(value: Any) -> str:
    """ISO UTC strings in this dataset sort chronologically after normalization."""
    if value is None:
        return ""
    return str(value).strip().replace("+00:00", "Z")


def flatten_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in flatten_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in flatten_values(child)]
    return [] if value is None else [str(value)]


@dataclass(frozen=True)
class Observation:
    evidence_id: str
    source_path: str
    location: str
    captured_at: str
    observed_at: str
    system_id: str
    resource_id: str
    category: str
    data: dict[str, Any]
    method: str
    claim: bool = False

    def available_at(self, cutoff: str) -> bool:
        return self.captured_at <= cutoff and (not self.observed_at or self.observed_at <= cutoff)


@dataclass
class EvidenceIndex:
    root: Path
    observations: list[Observation] = field(default_factory=list)
    extraction_errors: list[dict[str, str]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path) -> "EvidenceIndex":
        index = cls(root)
        dataset = root / "dataset"
        for item in read_csv(dataset / "evidence_files.csv"):
            index._load_file(item)
        return index

    def _load_file(self, item: dict[str, str]) -> None:
        # The manifest path is relative to dataset/, not the repository root.
        relative = Path(item["file_path"])
        path = (self.root / "dataset" / relative).resolve()
        if not path.is_relative_to((self.root / "dataset").resolve()):
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
        source = str(relative)
        captured = iso_at(item["captured_at"])
        self.documents.append({"file_id": item["file_id"], "source_path": source,
                               "captured_at": captured, "method": payload["method"],
                               "text": payload.get("text", ""), "errors": payload.get("errors", [])})
        for n, record in enumerate(payload.get("records", []), 1):
            data = record["data"]
            self.observations.append(Observation(
                evidence_id=item["file_id"], source_path=source,
                location=record.get("location", f"record:{n}"), captured_at=captured,
                observed_at=iso_at(record.get("observed_at", captured)),
                system_id=record.get("system_id", ""), resource_id=record.get("resource_id", ""),
                category=record.get("category", ""), data=data, method=payload["method"],
                claim=bool(record.get("claim", False)),
            ))

    def _extract_cached(self, path: Path) -> dict[str, Any]:
        digest = hashlib.sha256(Path(__file__).read_bytes() + path.read_bytes()).hexdigest()
        cache_dir = self.root / ".cache" / "ghosttrace" / "extraction"
        cache_file = cache_dir / f"{digest}.json"
        if cache_file.is_file():
            return json.loads(cache_file.read_text(encoding="utf-8"))
        suffix = path.suffix.lower()
        if suffix == ".json":
            payload = {"method": "json", "records": _json_records(json.loads(path.read_text(encoding="utf-8")))}
        elif suffix == ".sql":
            payload = {"method": "sqlite", "records": _sql_records(path)}
        elif suffix in {".log", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            payload = {"method": "text", "text": text, "records": _text_records(text)}
        elif suffix == ".pdf":
            payload = _pdf_extract(path)
        elif suffix in {".png", ".jpg", ".jpeg"}:
            payload = _image_extract(path)
        else:
            payload = {"method": "unsupported", "records": [], "errors": [f"unsupported type {suffix}"]}
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload


def _infer_system(resource: str, location: str, data: dict[str, Any]) -> str:
    explicit = data.get("system") or data.get("origin_system") or data.get("data_store")
    if explicit:
        return str(explicit)
    for name in ("account_service", "search_service", "analytics_platform", "support_platform",
                 "security_service", "integration_service", "backup_service", "export_service",
                 "identity_service", "marketing_platform"):
        if name in location:
            return name
    prefix = resource.split("_")[0]
    return {"account": "account_service", "profile": "account_service", "search": "search_service",
            "analytics": "analytics_platform", "support": "support_platform", "audit": "security_service",
            "backup": "backup_service", "export": "export_service", "data": "integration_service",
            "tombstone": "identity_service", "marketing": "marketing_platform"}.get(prefix, "")


def _infer_category(resource: str, location: str, data: dict[str, Any]) -> str:
    if data.get("record_type"):
        return str(data["record_type"])
    # A service-export wrapper is not itself a user export: prefer the ID prefix.
    text = resource.lower()
    if not RESOURCE.fullmatch(resource):
        text += " " + location.lower()
    for key, category in (("tombstone", "ACCOUNT_TOMBSTONE"), ("data_flow", "DATA_FLOW"),
                          ("connector", "DATA_FLOW"), ("backup", "BACKUP_ARCHIVE"),
                          ("restore", "BACKUP_ARCHIVE"), ("security", "SECURITY_AUDIT_LOG"),
                          ("audit_record", "SECURITY_AUDIT_LOG"), ("legal_hold", "LEGAL_HOLD_RECORD"),
                          ("support", "SUPPORT_MESSAGE"), ("analytics", "ANALYTICS_PROFILE"),
                          ("telemetry", "PRODUCT_TELEMETRY"), ("search", "SEARCH_INDEX_ENTRY"),
                          ("export", "USER_EXPORT"), ("marketing", "MARKETING_PROFILE"),
                          ("cache", "CACHE_ENTRY"), ("attachment", "SUPPORT_ATTACHMENT"),
                          ("email_log", "EMAIL_DELIVERY_LOG"), ("account", "USER_PROFILE"),
                          ("profile", "USER_PROFILE")):
        if key in text:
            return category
    return ""


def _json_records(obj: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def visit(value: Any, location: str, inherited: dict[str, Any]) -> None:
        if isinstance(value, list):
            for n, child in enumerate(value):
                visit(child, f"{location}[{n}]", inherited)
            return
        if not isinstance(value, dict):
            return
        context = dict(inherited)
        for key in ("captured_at", "exported_at", "data_store", "collection", "system", "origin"):
            if key in value and isinstance(value[key], str):
                context[key] = value[key]
        resource = str(value.get("resource_id") or "")
        if resource:
            data = dict(value)
            for key in ("data_store", "collection", "system"):
                if key in context and key not in data:
                    data[key] = context[key]
            observed = value.get("observed_at") or value.get("recorded_at") or value.get("captured_at") or context.get("captured_at") or context.get("exported_at")
            records.append({"data": data, "location": location, "resource_id": resource,
                            "system_id": _infer_system(resource, location, data),
                            "category": _infer_category(resource, location, data), "observed_at": observed})
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                # Nested document fields belong to the parent resource. A nested object
                # with a distinct resource_id is separately indexed.
                if resource and isinstance(child, dict) and child.get("resource_id") == resource:
                    continue
                visit(child, f"{location}.{key}", context)

    visit(obj, "$", {})
    return records


def _sql_records(path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(path.read_text(encoding="utf-8"))
        result: list[dict[str, Any]] = []
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            if table == "snapshot_scope":
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            for n, row in enumerate(conn.execute(f"SELECT * FROM {quoted}"), 1):
                data = dict(row)
                if isinstance(data.get("record_json"), str):
                    data.update(json.loads(data.pop("record_json")))
                resource = str(data.get("resource_id") or "")
                if not resource:
                    continue
                system = _infer_system(resource, table, data)
                result.append({"data": data, "location": f"table:{table}:row:{n}",
                               "resource_id": resource, "system_id": system,
                               "category": _infer_category(resource, table, data),
                               "observed_at": data.get("observed_at") or data.get("captured_at") or data.get("updated_at")})
        return result
    finally:
        conn.close()


def _text_records(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending: dict[str, dict[str, str]] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not RESOURCE.search(line):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        data: dict[str, Any] = {}
        for part in parts:
            if "=" in part:
                k, v = part.split("=", 1)
                data[k] = v
        if isinstance(data.get("message"), str) and "=" in data["message"]:
            try:
                nested_parts = shlex.split(data["message"])
            except ValueError:
                nested_parts = data["message"].split()
            for part in nested_parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                    data.setdefault(k, v)
        bracketed = re.match(r"\S+\s+\[[^]]+\]\s+\[([^]]+)\]\s+\[[^]]+\]\s+([a-z_]+):", line)
        if bracketed:
            data.setdefault("service", bracketed.group(1))
            data.setdefault("event", bracketed.group(2))
        if "function" in data and "service" not in data:
            data["service"] = data["function"]
        if "|" in line and "resource_id" not in data:
            chunks = [x.strip() for x in line.split("|")]
            if len(chunks) >= 5 and RESOURCE.fullmatch(chunks[2]) and re.search(r"\d{4}-\d\d-\d\d", chunks[0]):
                data["resource_id"] = chunks[2]
                data["system"] = chunks[1]
                data["record_type"] = chunks[3]
                data["result"] = chunks[4]
                if len(chunks) >= 6:
                    data["lookup_value"] = chunks[5]
        trace = data.get("trace_id", "")
        if trace:
            previous = pending.setdefault(trace, {})
            for key in ("lookup_key", "subject_key", "match_key", "resource_id"):
                if key in data:
                    previous[key] = str(data[key])
            data = {**previous, **data}
        resource = str(data.get("resource_id") or (RESOURCE.search(line).group() if RESOURCE.search(line) else ""))
        if not resource:
            continue
        event = str(data.get("event") or "")
        if event.endswith("_started") or event in {"connection_acquired", "dependency_retry", "dependency_recovered"}:
            continue
        match = re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line)
        stamp = match.group() if match else ""
        data["raw_line"] = line
        result.append({"data": data, "location": f"line:{n}", "resource_id": resource,
                       "system_id": _infer_system(resource, "line", data),
                       "category": _infer_category(resource, "line", data),
                       "observed_at": stamp or data.get("recorded_at")})
    return result


def _pdf_extract(path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                              text=True, check=True, timeout=60)
        text = proc.stdout
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return {"method": "pdf_text", "text": "", "records": [], "errors": [str(exc)]}
    if text.strip():
        return {"method": "pdf_text", "text": text, "records": _text_records(text)}
    try:
        with tempfile.TemporaryDirectory(prefix="ghosttrace-pdf-") as temp:
            prefix = str(Path(temp) / "page")
            subprocess.run(["pdftoppm", "-f", "1", "-l", "20", "-r", "200", "-png",
                            str(path), prefix], capture_output=True, check=True, timeout=120)
            pages = sorted(Path(temp).glob("page-*.png"))
            if not pages:
                return {"method": "pdf_ocr", "text": "", "records": [],
                        "errors": ["PDF rasterization produced no pages"]}
            text = "\n".join(subprocess.run(["tesseract", str(page), "stdout"], capture_output=True,
                                            text=True, check=True, timeout=90).stdout for page in pages)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return {"method": "pdf_ocr", "text": "", "records": [], "errors": [str(exc)]}
    return {"method": "pdf_ocr", "text": text, "records": _text_records(text),
            "errors": [] if text.strip() else ["PDF OCR produced no text"]}


def _image_extract(path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(["tesseract", str(path), "stdout"], capture_output=True,
                              text=True, check=True, timeout=90)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return {"method": "ocr", "text": "", "records": [], "errors": [str(exc)]}
    text = proc.stdout
    return {"method": "ocr", "text": text, "records": _text_records(text),
            "errors": [] if text.strip() else ["OCR produced no text"]}
