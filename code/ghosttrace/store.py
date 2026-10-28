"""An on-disk index of every record in the supplied files.

A large company's exports do not fit in memory. Each file is read once, record
by record, into a local SQLite file. For every record the index keeps the
values a person can be found by:
  e:  email addresses
  p:  phone numbers (their last nine digits)
  h:  hex digests (SHA-256, SHA-1, MD5)
  t:  tokens of four or more characters that contain a digit, which covers
      most IDs ("C-10442", "10442")
  o:  OCR text that holds an email, or a "no results" message
A check looks these up instead of reading every row again, and then confirms
each candidate with the scanner's exact matching rules. Names, and IDs without
a digit, are found by a text search over the stored records instead.

A file is read again only when its contents or the reading rules change. The
index stays on this machine.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable
import unicodedata

from .classify import column_kind
from .ingest import EMAIL
from .readers import Record, Source, file_time, kind_of, read_into

PHONE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
HEX = re.compile(r"\b(?:[0-9a-f]{64}|[0-9a-f]{40}|[0-9a-f]{32})\b", re.I)
TOKEN = re.compile(r"[\w-]+")
DIGIT_TOKEN = re.compile(r"(?<![\w-])[\w-]*\d[\w-]*")   # whole tokens that contain a digit
DATE = re.compile(r"\d{4}-\d\d-\d\d")
NOT_DIGIT = re.compile(r"\D")
OCR_METHODS = {"ocr", "pdf_ocr"}
DOCUMENT_METHODS = {"ocr", "pdf_text", "pdf_ocr", "docx", "pptx"}
KEY_FIELDS = ("resource_id", "record_id", "id", "uuid", "ticket_id", "order_id", "invoice_id", "export_id")
# A search screen that found nothing: "No customer found", "0 results", "User not found".
NO_RESULT = re.compile(r"\b(?:no|0|zero)\s+(?:matching\s+)?(?:results?|records?|matches|customers?|users?|accounts?|"
                       r"profiles?|members?|contacts?|people|persons?|entries)\b(?!\s+(?:yet|selected))|"
                       r"\b(?:customer|user|account|record|profile|member|contact|person)\s+(?:was\s+)?not\s+found\b|"
                       r"\bnothing\s+found\b|\bno\s+match\s+found\b", re.I)
BATCH = 5000          # records written to the index at a time
CHUNK = 500           # record IDs fetched per query

# Reading rules: a change to any of these files means every file is read again.
VERSION = hashlib.sha256(b"".join((Path(__file__).parent / name).read_bytes()
                                  for name in ("readers.py", "store.py", "classify.py", "ingest.py"))).hexdigest()[:12]

LAYOUT = 2   # bumped when the tables change; an index with another layout is rebuilt
# Records refer to their file by number. page, entity and trace are stored only
# when a record has one (document pages, record IDs, log traces).
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, digest TEXT UNIQUE, kind TEXT, records INTEGER,
                                  containers TEXT, errors TEXT, notes TEXT, complete INTEGER);
CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, digest TEXT);
CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, file INTEGER, location TEXT, container TEXT, fields TEXT,
                                    text TEXT, method TEXT, observed_at TEXT, confidence REAL, page TEXT,
                                    entity TEXT, trace TEXT, search TEXT);
CREATE TABLE IF NOT EXISTS keys (key TEXT, record INTEGER, PRIMARY KEY (key, record)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS records_by_file ON records (file);
CREATE INDEX IF NOT EXISTS records_by_page ON records (file, page) WHERE page IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_by_entity ON records (file, entity) WHERE entity IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_by_trace ON records (file, trace) WHERE trace IS NOT NULL;
"""
COLUMNS = "id, file, location, container, fields, text, method, observed_at, confidence"


def phone_key(digits: str) -> str:
    """Phone numbers are compared on their last nine digits (national vs international format)."""
    return digits[-9:] if len(digits) >= 9 else digits


def fold(text: str) -> str:
    """Lower case without accents, for text search."""
    if not text.isascii():
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return text.lower()


def indexed_token(token: str) -> bool:
    """A lower-case token that is looked up in the index: four or more characters with a
    digit, and not a date or a digest. Other IDs are found by text search."""
    return (len(token) >= 4 and TOKEN.fullmatch(token) is not None and DIGIT_TOKEN.fullmatch(token) is not None
            and not DATE.match(token) and not HEX.fullmatch(token))


def page_of(location: str) -> str:
    """'page 2, line 14' -> 'page 2'; screenshot and document lines -> the whole file.
    Archive members keep their name: 'scans.zip member.pdf :: page 2'."""
    member, sep, place = location.rpartition(" :: ")
    prefix = member + sep
    match = re.match(r"(page \d+)", place)
    if match:
        return prefix + match.group(1)
    if place.startswith("slide"):
        return prefix + place.split(",")[0]
    return prefix + ("whole image" if place.startswith("OCR") else "whole document")


def entity_key(record: Record) -> str:
    """What makes two observations the same stored copy: a record ID, else the place."""
    if record.fields:
        lowered = {k.lower().split(".")[-1]: v for k, v in record.fields.items()}
        for name in KEY_FIELDS:
            if lowered.get(name):
                return f"{name}={lowered[name]}"
    return record.location


def record_keys(record: Record, kinds: dict[str, str]) -> set[str]:
    """The lookup keys of one record. kinds remembers each column's identifier kind.

    The values are joined with " | ", which none of the patterns can match across, so
    each pattern runs once per record and still finds what it would in each value."""
    keys = set()
    values = [str(v) for v in record.fields.values()] + ([record.text] if record.text else [])
    text = " | ".join(values)
    lowered = text.lower()
    has_email = False
    if "@" in text:
        for email in EMAIL.findall(text):
            keys.add("e:" + email.lower())
            has_email = True
    candidates = PHONE.findall(text)
    for column, value in record.fields.items():
        if column not in kinds:
            kinds[column] = column_kind(column)
        if kinds[column] == "phone":
            candidates.append(str(value))
    for candidate in candidates:
        digits = NOT_DIGIT.sub("", candidate)
        if len(digits) >= 7 and not DATE.match(candidate.strip()):
            keys.add("p:" + phone_key(digits))
    if len(text) >= 32:
        for digest in HEX.findall(lowered):
            keys.add("h:" + digest)
    for token in DIGIT_TOKEN.findall(lowered):
        if len(token) >= 4 and not DATE.match(token) and not HEX.fullmatch(token):
            keys.add("t:" + token)
    if record.method in OCR_METHODS:
        if has_email:
            keys.add("o:email")
        if record.text and NO_RESULT.search(record.text):
            keys.add("o:no-result")
    return keys


class Store:
    """The index file. One lock serializes access, so the web server's threads can share it."""

    def __init__(self, path: Path | None = None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            if path:
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            if self.db.execute("PRAGMA user_version").fetchone()[0] != LAYOUT:
                for table in ("files", "seen", "records", "keys"):
                    self.db.execute(f"DROP TABLE IF EXISTS {table}")
                self.db.execute(f"PRAGMA user_version = {LAYOUT}")
            self.db.executescript(SCHEMA)
            self.next_id = (self.db.execute("SELECT max(id) FROM records").fetchone()[0] or 0) + 1
            self.drop_old_versions()

    def close(self) -> None:
        self.db.close()

    def __del__(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    # -- building ------------------------------------------------------------

    def digest(self, path: Path) -> str:
        """The file's content digest, remembered by path, size and modified time."""
        stat = path.stat()
        where = str(path.resolve())
        row = self.db.execute("SELECT size, mtime, digest FROM seen WHERE path = ?", (where,)).fetchone()
        if row and row[0] == stat.st_size and row[1] == stat.st_mtime and row[2].startswith(VERSION):
            return row[2]
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                hasher.update(chunk)
        digest = f"{VERSION}:{hasher.hexdigest()}"
        self.db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)", (where, stat.st_size, stat.st_mtime, digest))
        self.db.commit()
        return digest

    def file_row(self, digest: str) -> tuple[Any, ...] | None:
        """(id, kind, records, containers, errors, notes) of a fully read file."""
        return self.db.execute("SELECT id, kind, records, containers, errors, notes FROM files "
                               "WHERE digest = ? AND complete = 1", (digest,)).fetchone()

    def source(self, path: Path, name: str, captured_at: str = "",
               progress: Callable[..., None] | None = None) -> Source:
        """A Source for the file, reading it into the index first if needed."""
        with self.lock:
            digest = self.digest(path)
            row = self.file_row(digest)
            if row is None:
                self.build(path, name, digest, progress)
                row = self.file_row(digest)
        source = Source(name, row[1], path.stat().st_size, captured_at or file_time(path))
        source.count, source.containers = row[2], json.loads(row[3])
        source.errors, source.notes = json.loads(row[4]), json.loads(row[5])
        source.digest, source.store = digest, self
        return source

    def build(self, path: Path, name: str, digest: str, progress: Callable[..., None] | None) -> None:
        self.forget(digest)   # leftovers of an interrupted run
        file_id = self.db.execute("INSERT INTO files (digest, complete) VALUES (?, 0)", (digest,)).lastrowid
        started = time.time()
        source = Source(name, kind_of(path), path.stat().st_size, "")
        pending: list[Record] = []
        kinds: dict[str, str] = {}

        def flush() -> None:
            rows, keys = [], []
            for record in pending:
                values = [str(v) for v in record.fields.values()] + [record.text]
                joined = "\n".join(values)
                entity = entity_key(record)
                rows.append((self.next_id, file_id, record.location, record.container,
                             json.dumps(record.fields, ensure_ascii=False) if record.fields else "", record.text,
                             record.method, record.observed_at, record.confidence,
                             page_of(record.location) if record.method in DOCUMENT_METHODS else None,
                             entity if entity != record.location else None,
                             record.fields.get("trace_id") or None,
                             None if joined.isascii() else fold(joined)))
                keys.extend((key, self.next_id) for key in record_keys(record, kinds))
                self.next_id += 1
            self.db.executemany("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self.db.executemany("INSERT OR IGNORE INTO keys VALUES (?, ?)", keys)
            self.db.commit()   # small steps keep the write-ahead log small
            pending.clear()
            if progress:
                progress(name, source.count)

        def add(record: Record) -> None:
            pending.append(record)
            if len(pending) >= BATCH:
                flush()

        source.sink = add
        try:
            read_into(source, path)
        except Exception as exc:   # a broken file must not stop the other sources
            source.errors.append(f"could not read: {type(exc).__name__}: {exc}")
        flush()
        self.db.execute("UPDATE files SET kind = ?, records = ?, containers = ?, errors = ?, notes = ?, complete = 1 "
                        "WHERE id = ?", (source.kind, source.count, json.dumps(sorted(source.containers)),
                                         json.dumps(source.errors), json.dumps(source.notes), file_id))
        self.db.commit()
        if progress:
            progress(name, source.count, time.time() - started)

    def forget(self, digest: str) -> None:
        with self.lock:
            for (file_id,) in self.db.execute("SELECT id FROM files WHERE digest = ?", (digest,)).fetchall():
                self.db.execute("DELETE FROM keys WHERE record IN (SELECT id FROM records WHERE file = ?)", (file_id,))
                self.db.execute("DELETE FROM records WHERE file = ?", (file_id,))
                self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self.db.commit()

    def prune(self, keep: set[str]) -> None:
        """Drop the index of files no longer in use."""
        with self.lock:
            for (digest,) in self.db.execute("SELECT digest FROM files").fetchall():
                if digest not in keep:
                    self.forget(digest)

    def drop_old_versions(self) -> None:
        for (digest,) in self.db.execute("SELECT digest FROM files").fetchall():
            if not digest.startswith(VERSION):
                self.forget(digest)

    # -- looking up ----------------------------------------------------------

    def file_ids(self, digests: set[str]) -> dict[int, str]:
        """File number -> digest for the given files."""
        out = {}
        for digest in digests:
            row = self.db.execute("SELECT id FROM files WHERE digest = ?", (digest,)).fetchone()
            if row:
                out[row[0]] = digest
        return out

    def records(self, ids: set[int], files: dict[int, str]) -> list[tuple[str, int, Record]]:
        """(file digest, record ID, record) in file order."""
        out = []
        ordered = sorted(ids)
        for start in range(0, len(ordered), CHUNK):
            part = ordered[start:start + CHUNK]
            marks = ", ".join("?" * len(part))
            for row in self.db.execute(f"SELECT {COLUMNS} FROM records WHERE id IN ({marks}) ORDER BY id", part):
                if row[1] in files:
                    out.append((files[row[1]], row[0], to_record(row)))
        return out

    def find(self, digests: set[str], keys: list[str], phrases: list[list[str]] = ()) -> list[tuple[str, int, Record]]:
        """Records in the given files that hold any of the keys, or whose text contains the
        words of any phrase in that order (lower case, no accents)."""
        with self.lock:
            files = self.file_ids(digests)
            ids = set()
            for key in keys:
                for (record_id,) in self.db.execute("SELECT record FROM keys WHERE key = ?", (key,)):
                    ids.add(record_id)
            for words in phrases:
                pattern = "%" + "%".join(w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                                         for w in words) + "%"
                for file_id in sorted(files):
                    for (record_id,) in self.db.execute(
                            "SELECT id FROM records WHERE file = ? AND "
                            "COALESCE(search, fields || char(10) || text) LIKE ? ESCAPE '\\'", (file_id, pattern)):
                        ids.add(record_id)
            return self.records(ids, files)

    def sample(self, digest: str, limit: int = 50, offset: int = 0) -> list[Record]:
        """Records of one file in file order, for a preview."""
        with self.lock:
            files = self.file_ids({digest})
            return [to_record(row) for row in self.db.execute(
                f"SELECT {COLUMNS} FROM records WHERE file = ? ORDER BY id LIMIT ? OFFSET ?",
                (next(iter(files), -1), limit, offset))]

    def page(self, digest: str, page: str) -> list[Record]:
        """Every record on one page of a document, in reading order."""
        with self.lock:
            files = self.file_ids({digest})
            return [to_record(row) for row in self.db.execute(
                f"SELECT {COLUMNS} FROM records WHERE file = ? AND page = ? ORDER BY id", (next(iter(files), -1), page))]

    def trace(self, digest: str, trace: str) -> list[Record]:
        """Every log record of one trace, in file order."""
        with self.lock:
            files = self.file_ids({digest})
            return [to_record(row) for row in self.db.execute(
                f"SELECT {COLUMNS} FROM records WHERE file = ? AND trace = ? ORDER BY id", (next(iter(files), -1), trace))]

    def has_entity(self, digest: str, entity: str) -> bool:
        """Whether the file holds the same stored copy: the same record ID, or the same place."""
        column = "entity" if entity.partition("=")[0] in KEY_FIELDS else "location"
        with self.lock:
            files = self.file_ids({digest})
            return self.db.execute(f"SELECT 1 FROM records WHERE file = ? AND {column} = ? LIMIT 1",
                                   (next(iter(files), -1), entity)).fetchone() is not None


def to_record(row: tuple[Any, ...]) -> Record:
    return Record("", row[2], row[3], json.loads(row[4]) if row[4] else {}, text=row[5], method=row[6],
                  observed_at=row[7], confidence=row[8])
