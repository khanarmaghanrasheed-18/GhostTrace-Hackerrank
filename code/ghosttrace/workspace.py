"""A local folder holding the files to check, their settings and saved checks.

Layout:
  <workspace>/sources/        the supplied files, exactly as uploaded
  <workspace>/sources.json    per-file settings: captured_at, category, store, complete
  <workspace>/checks/         saved check results (JSON)
  <workspace>/policy.csv      optional custom deletion policy
  <workspace>/settings.json   local settings, e.g. whether the AI helper is allowed
  <workspace>/.cache/         parsed-file cache
Everything stays on this machine.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import threading
from typing import Any

from .classify import CATEGORIES, load_policy
from .readers import Source, iso_time
from .scanner import load_sources, read_manifest


def safe_name(name: str) -> str:
    """A plain file name: no folders, no hidden files, no odd characters."""
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(" .")
    return base or "upload"


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.sources_dir = self.root / "sources"
        self.checks_dir = self.root / "checks"
        self.cache_dir = self.root / ".cache"
        for folder in (self.sources_dir, self.checks_dir, self.cache_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self._sources: list[Source] | None = None
        self._lock = threading.Lock()   # one reader at a time; OCR is slow and parsed results are cached

    # -- files ---------------------------------------------------------------

    def meta(self) -> dict[str, dict[str, Any]]:
        path = self.root / "sources.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def save_meta(self, meta: dict[str, dict[str, Any]]) -> None:
        (self.root / "sources.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self._sources = None

    def add_source(self, name: str, data: bytes) -> str:
        name = safe_name(name)
        target = self.sources_dir / name
        stem, suffix, n = target.stem, target.suffix, 2
        while target.exists():
            target = self.sources_dir / f"{stem} ({n}){suffix}"
            n += 1
        target.write_bytes(data)
        self._sources = None
        return target.name

    def remove_source(self, name: str) -> bool:
        target = self.sources_dir / safe_name(name)
        if target.is_file() and target.parent == self.sources_dir:
            target.unlink()
            meta = self.meta()
            meta.pop(target.name, None)
            self.save_meta(meta)
            return True
        return False

    def update_source(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        name = safe_name(name)
        meta = self.meta()
        entry = meta.get(name, {})
        if "captured_at" in changes:
            entry["captured_at"] = iso_time(changes["captured_at"]) if changes["captured_at"] else ""
        if "category" in changes:
            category = str(changes["category"] or "").upper()
            entry["category"] = category if category in CATEGORIES else ""
        if "store" in changes:
            entry["store"] = str(changes["store"] or "").strip()[:120]
        if "complete" in changes:
            entry["complete"] = bool(changes["complete"])
        meta[name] = {k: v for k, v in entry.items() if v not in ("", None, False)}
        self.save_meta(meta)
        return meta[name]

    def import_manifest(self, data: bytes) -> int:
        """Read capture times (and optional category/store) from a CSV manifest."""
        path = self.cache_dir / "manifest_upload.csv"
        path.write_bytes(data)
        found = read_manifest(path)
        path.unlink()
        meta = self.meta()
        names = {p.name for p in self.sources_dir.iterdir() if p.is_file()}
        count = 0
        for name, entry in found.items():
            if name in names:
                meta.setdefault(name, {}).update({k: v for k, v in entry.items() if v})
                count += 1
        self.save_meta(meta)
        return count

    def sources(self) -> list[Source]:
        with self._lock:
            if self._sources is None:
                self._sources = load_sources([self.sources_dir], self.cache_dir, self.meta(), base=self.sources_dir)
            return self._sources

    def source_list(self) -> list[dict[str, Any]]:
        meta = self.meta()
        out = []
        for source in self.sources():
            out.append({**source.summary(), **{"settings": meta.get(source.name, {})}})
        return out

    # -- checks --------------------------------------------------------------

    def save_check(self, request: dict[str, Any], result: dict[str, Any]) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        check_id, n = f"check-{stamp}", 2
        while (self.checks_dir / f"{check_id}.json").exists():
            check_id = f"check-{stamp}-{n}"
            n += 1
        saved = {"id": check_id, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "request": request, "result": result}
        (self.checks_dir / f"{check_id}.json").write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        return check_id

    def get_check(self, check_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"check-[\w-]+", check_id):
            return None
        path = self.checks_dir / f"{check_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def list_checks(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.checks_dir.glob("check-*.json"), reverse=True):
            data = json.loads(path.read_text(encoding="utf-8"))
            result = data["result"]
            out.append({"id": data["id"], "created_at": data["created_at"], "request": data["request"],
                        "status": result["status"], "status_text": result["status_text"], "counts": result["counts"],
                        "categories": sorted({f["category"] for f in result["findings"] if f["verdict"] == "violation"})})
        return out

    def delete_check(self, check_id: str) -> bool:
        path = self.checks_dir / f"{check_id}.json"
        if re.fullmatch(r"check-[\w-]+", check_id) and path.is_file():
            path.unlink()
            return True
        return False

    # -- policy and settings ---------------------------------------------------

    def policy_file(self) -> Path | None:
        path = self.root / "policy.csv"
        return path if path.is_file() else None

    def set_policy(self, data: bytes) -> list[dict[str, str]]:
        text = data.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or not {"data_category", "required_action"} <= set(rows[0]):
            raise ValueError("a policy CSV needs data_category and required_action columns")
        rows = [r for r in rows if r.get("data_category")]
        bad = [r["required_action"] for r in rows if r["required_action"].strip().upper()
               not in ("DELETE", "REDACT", "ANONYMIZE", "RETAIN", "DISABLE")]
        if bad:
            raise ValueError(f"unknown required_action values: {', '.join(sorted(set(bad)))}")
        (self.root / "policy.csv").write_text(text, encoding="utf-8")
        return self.policy()

    def reset_policy(self) -> None:
        path = self.root / "policy.csv"
        if path.is_file():
            path.unlink()

    def policy(self) -> list[dict[str, str]]:
        rules = load_policy(self.policy_file())
        return [rules[k] for k in sorted(rules)]

    def settings(self) -> dict[str, Any]:
        path = self.root / "settings.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"ai_allowed": False}

    def save_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        if "ai_allowed" in changes:
            settings["ai_allowed"] = bool(changes["ai_allowed"])
        (self.root / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return settings
