"""A local folder holding the files to check, their settings and saved checks.

Layout:
  <workspace>/sources/        the supplied files, exactly as uploaded
  <workspace>/folders.json    folders read in place, never copied (for large data)
  <workspace>/sources.json    per-file settings: captured_at, category, store, complete
  <workspace>/checks/         saved check results (JSON)
  <workspace>/batches/        saved batch results (JSON)
  <workspace>/policy.csv      optional custom deletion policy
  <workspace>/.cache/         the index of every record (index.sqlite)
Everything stays on this machine.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import shutil
import sys
import threading
from typing import Any, BinaryIO

from .classify import CATEGORIES, load_policy
from .readers import Source, iso_time
from .scanner import collect_files, load_sources, read_manifest
from .store import Store


def show_progress(name: str, count: int, seconds: float | None = None) -> None:
    """Progress of reading a new or changed file into the index, on the terminal."""
    if seconds is None:
        print(f"\r  reading {name}: {count:,} records", end="", file=sys.stderr, flush=True)
    else:
        print(f"\r  read {name}: {count:,} records in {seconds:.0f}s", file=sys.stderr, flush=True)


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
        self.batches_dir = self.root / "batches"
        self.cache_dir = self.root / ".cache"
        for folder in (self.sources_dir, self.checks_dir, self.batches_dir, self.cache_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.cache_dir / "index.sqlite")
        self._sources: list[Source] | None = None
        self._listed: list[Source] = []     # the last complete list, shown while files are being read
        self._generation = 0                # bumped whenever the files or their settings change
        self._lock = threading.Lock()       # one reader at a time; OCR is slow and the index keeps what was read

    def invalidate(self) -> None:
        """The files or their settings changed: the next sources() reads what is new."""
        self._generation += 1
        self._sources = None

    def busy(self) -> bool:
        """Whether files are being read into the index right now."""
        return self._lock.locked()

    def ready(self) -> bool:
        return self._sources is not None

    # -- files ---------------------------------------------------------------

    def meta(self) -> dict[str, dict[str, Any]]:
        path = self.root / "sources.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def save_meta(self, meta: dict[str, dict[str, Any]]) -> None:
        (self.root / "sources.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.invalidate()

    def new_target(self, name: str) -> Path:
        target = self.sources_dir / safe_name(name)
        stem, suffix, n = target.stem, target.suffix, 2
        while target.exists():
            target = self.sources_dir / f"{stem} ({n}){suffix}"
            n += 1
        return target

    def add_source(self, name: str, data: bytes) -> str:
        target = self.new_target(name)
        target.write_bytes(data)
        self.invalidate()
        return target.name

    def add_upload(self, name: str, stream: BinaryIO, length: int) -> str:
        """Write an upload to disk as it arrives, so a large file never sits in memory."""
        target = self.new_target(name)
        with target.open("wb") as out:
            left = length
            while left > 0:
                chunk = stream.read(min(left, 1 << 20))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        if left > 0:
            target.unlink()
            raise ValueError("the upload was interrupted")
        self.invalidate()
        return target.name

    def copy_source(self, path: Path) -> str:
        target = self.new_target(path.name)
        shutil.copyfile(path, target)
        self.invalidate()
        return target.name

    def remove_source(self, name: str) -> bool:
        """Delete an uploaded file. Files in linked folders are never deleted; unlink the folder instead."""
        if "/" in name or "\\" in name:
            return False
        target = self.sources_dir / safe_name(name)
        if target.is_file() and target.parent == self.sources_dir:
            target.unlink()
            meta = self.meta()
            meta.pop(target.name, None)
            self.save_meta(meta)
            self.store.prune({s.digest for s in self.sources()})
            return True
        return False

    # -- folders read in place -------------------------------------------------

    def folders(self) -> list[str]:
        path = self.root / "folders.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def link_folder(self, folder: str) -> list[str]:
        path = Path(folder).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"no such folder or file: {path}")
        if path == self.root or self.root in path.parents:
            raise ValueError("that is inside the workspace itself")
        if path in self.root.parents:
            raise ValueError("that folder contains the workspace; link a narrower folder or keep the workspace elsewhere")
        folders = self.folders()
        if str(path) not in folders:
            folders.append(str(path))
            (self.root / "folders.json").write_text(json.dumps(folders, indent=2), encoding="utf-8")
            self.invalidate()
        return folders

    def unlink_folder(self, folder: str) -> list[str]:
        folders = [f for f in self.folders() if f != folder]
        (self.root / "folders.json").write_text(json.dumps(folders, indent=2), encoding="utf-8")
        self.invalidate()
        self.store.prune({s.digest for s in self.sources()})
        return folders

    def update_source(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        if name not in self.file_names():
            raise ValueError("unknown source")
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
        count = 0
        for name in self.file_names():
            entry = found.get(Path(name).name)
            if entry:
                meta.setdefault(name, {}).update({k: v for k, v in entry.items() if v})
                count += 1
        self.save_meta(meta)
        return count

    def roots(self) -> list[tuple[Path, Path, str]]:
        """(path, base, name prefix) for the uploads folder and each linked folder that exists.
        Files are named relative to base, after the prefix ('folder/relative path')."""
        out = [(self.sources_dir, self.sources_dir, "")]
        used = set()
        for folder in self.folders():
            path = Path(folder)
            if not path.exists():
                continue
            base = path if path.is_dir() else path.parent
            prefix, n = base.name or "folder", 2
            while prefix in used:   # two linked folders with the same name
                prefix, n = f"{base.name} ({n})", n + 1
            used.add(prefix)
            out.append((path, base, prefix + "/"))
        return out

    def file_names(self) -> list[str]:
        """The names every file will have, without reading any of them."""
        return [prefix + found.relative_to(base).as_posix()
                for path, base, prefix in self.roots() for found in collect_files([path])]

    def sources(self, progress: Any = None, done: Any = None) -> list[Source]:
        """Uploaded files, then the files of each linked folder (named 'folder/relative path').

        progress(name, records, seconds=None) reports a file being read; done(source) is
        called as each file is ready. Both are optional; progress is also shown in the terminal."""

        def report(name: str, count: int, seconds: float | None = None) -> None:
            show_progress(name, count, seconds)
            if progress:
                progress(name, count, seconds)

        with self._lock:
            if self._sources is None:
                generation = self._generation
                meta = self.meta()
                found = []
                for path, base, prefix in self.roots():
                    found += load_sources([path], None, meta, base=base, store=self.store, prefix=prefix,
                                          progress=report, done=done)
                self._listed = found
                if generation == self._generation:   # nothing changed while reading
                    self._sources = found
                return found
            return self._sources

    def file_count(self) -> int:
        """How many files a full read covers (uploaded files and linked folders)."""
        return len(self.file_names())

    def source_list(self, wait: bool = True) -> list[dict[str, Any]]:
        """Every file with its settings. With wait=False this never blocks: while files are
        being read it shows the last complete list, and new uploads as 'waiting'."""
        meta = self.meta()
        if wait or (self._sources is not None and not self.busy()):
            sources, current = self.sources(), True
        else:
            sources, current = (self._sources or self._listed), False
        out = []
        for source in sources:
            out.append({**source.summary(), "settings": meta.get(source.name, {}), "linked": "/" in source.name,
                        "status": "ready"})
        if not current:
            known = {s["name"] for s in out}
            for path in collect_files([self.sources_dir]):
                if path.name not in known:
                    out.append({"name": path.name, "kind": "", "size": path.stat().st_size, "captured_at": "",
                                "records": 0, "containers": [], "errors": [], "notes": [], "readable": True,
                                "settings": meta.get(path.name, {}), "linked": False, "status": "waiting"})
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

    # -- batches -------------------------------------------------------------

    def save_batch(self, name: str, rows: int, results: list[dict[str, Any]]) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        batch_id, n = f"batch-{stamp}", 2
        while (self.batches_dir / f"{batch_id}.json").exists():
            batch_id = f"batch-{stamp}-{n}"
            n += 1
        saved = {"id": batch_id, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "file": name, "rows": rows, "results": results}
        (self.batches_dir / f"{batch_id}.json").write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        return batch_id

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"batch-[\w-]+", batch_id):
            return None
        path = self.batches_dir / f"{batch_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def list_batches(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.batches_dir.glob("batch-*.json"), reverse=True):
            data = json.loads(path.read_text(encoding="utf-8"))
            counts: dict[str, int] = {}
            for row in data["results"]:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            out.append({"id": data["id"], "created_at": data["created_at"], "file": data["file"],
                        "rows": data["rows"], "by_status": counts})
        return out

    def delete_batch(self, batch_id: str) -> bool:
        path = self.batches_dir / f"{batch_id}.json"
        if re.fullmatch(r"batch-[\w-]+", batch_id) and path.is_file():
            path.unlink()
            return True
        return False

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

    def set_rules(self, rules: list[dict[str, str]]) -> list[dict[str, str]]:
        """Save rules edited on the page; only rules that differ from the default are kept."""
        default = {r["data_category"]: r for r in load_policy(None).values()}
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["rule_id", "data_category", "condition", "required_action"])
        changed = 0
        for rule in rules:
            category = str(rule.get("data_category", "")).strip().upper()
            action = str(rule.get("required_action", "")).strip().upper()
            if category not in CATEGORIES:
                raise ValueError(f"unknown kind of data: {category or '(empty)'}")
            base = default.get(category, {})
            if action == base.get("required_action") and str(rule.get("condition", "")) == base.get("condition", ""):
                continue
            writer.writerow([str(rule.get("rule_id") or base.get("rule_id") or f"C-{category}"), category,
                             str(rule.get("condition", "")), action])
            changed += 1
        if not changed:
            self.reset_policy()
            return self.policy()
        return self.set_policy(out.getvalue().encode("utf-8"))

    def reset_policy(self) -> None:
        path = self.root / "policy.csv"
        if path.is_file():
            path.unlink()

    def policy(self) -> list[dict[str, str]]:
        rules = load_policy(self.policy_file())
        return [rules[k] for k in sorted(rules)]
