"""Read any supplied file into plain records, keeping where each value came from.

Supported without extra Python packages:
  spreadsheets  .csv .tsv .xlsx .xlsm
  databases     SQLite files (.db .sqlite .sqlite3) and SQL dumps (.sql)
  structured    .json .jsonl .ndjson .xml
  documents     .pdf (text layer, OCR fallback) .docx .pptx .eml .txt .md .html .log
  images        .png .jpg .jpeg .tif .tiff .bmp .gif .webp (OCR)
  archives      .zip (members are read with the same rules)
PDF and image text needs the command-line tools pdftotext/pdftoppm (poppler-utils)
and tesseract. A file that cannot be read is reported, never treated as empty.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from typing import Any
from xml.etree import ElementTree
import zipfile

from .ingest import parse_line

MAX_RECORDS_PER_SOURCE = 200_000
MAX_FILE_BYTES = 300 * 1024 * 1024

SPREADSHEET = {".csv", ".tsv", ".xlsx", ".xlsm"}
DATABASE = {".db", ".sqlite", ".sqlite3", ".sql"}
STRUCTURED = {".json", ".jsonl", ".ndjson", ".xml"}
DOCUMENT = {".pdf", ".docx", ".pptx", ".eml", ".txt", ".md", ".html", ".htm", ".log", ".text"}
IMAGE = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
ARCHIVE = {".zip"}
SUPPORTED = SPREADSHEET | DATABASE | STRUCTURED | DOCUMENT | IMAGE | ARCHIVE
TIME_FIELDS = ("updated_at", "modified_at", "last_modified", "observed_at", "captured_at", "recorded_at",
               "event_time", "timestamp", "time", "date", "created_at")
ISO_TIME = re.compile(r"\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?)?")


@dataclass
class Record:
    """One row, object, line or page of a source."""
    source: str                 # file name as supplied (archive members: archive/member)
    location: str               # human-readable place inside the source
    container: str = ""         # sheet, table or collection name
    fields: dict[str, str] = field(default_factory=dict)   # column -> value for structured data
    text: str = ""              # free text for documents, logs and OCR
    method: str = ""            # how it was read
    observed_at: str = ""       # time stated by the record itself, if any
    confidence: float = 1.0     # OCR confidence 0..1; 1.0 for exact parsing

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Source:
    name: str
    kind: str
    size: int
    captured_at: str
    records: list[Record] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        containers = sorted({r.container for r in self.records if r.container})
        return {"name": self.name, "kind": self.kind, "size": self.size, "captured_at": self.captured_at,
                "records": len(self.records), "containers": containers, "errors": self.errors,
                "notes": self.notes, "readable": not self.errors or bool(self.records)}


def file_time(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_time(value: Any) -> str:
    """A record's own timestamp as ISO text, or '' when the value is not a date."""
    text = str(value or "").strip()
    match = ISO_TIME.match(text)
    if not match:
        return ""
    found = match.group().replace(" ", "T")
    if len(found) == 10:
        found += "T00:00:00"
    if len(found) == 16:
        found += ":00"
    return found + "Z"


def record_time(fields: dict[str, str]) -> str:
    lowered = {k.lower(): v for k, v in fields.items()}
    for name in TIME_FIELDS:
        if name in lowered and iso_time(lowered[name]):
            return iso_time(lowered[name])
    return ""


def kind_of(path: Path) -> str:
    suffix = path.suffix.lower()
    for kind, group in (("spreadsheet", SPREADSHEET), ("database", DATABASE), ("structured", STRUCTURED),
                        ("document", DOCUMENT), ("image", IMAGE), ("archive", ARCHIVE)):
        if suffix in group:
            return kind
    if path.is_file() and path.read_bytes()[:16].startswith(b"SQLite format 3"):
        return "database"
    return "unsupported"


# ---------------------------------------------------------------------------
# Entry point with a digest-keyed cache.

def read_source(path: Path, name: str | None = None, cache_dir: Path | None = None,
                captured_at: str = "") -> Source:
    name = name or path.name
    size = path.stat().st_size
    source = Source(name, kind_of(path), size, captured_at or file_time(path))
    if size > MAX_FILE_BYTES:
        source.errors.append(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB; split it first")
        return source
    cache_file = None
    if cache_dir:
        digest = hashlib.sha256(Path(__file__).read_bytes() + path.read_bytes()).hexdigest()
        cache_file = cache_dir / f"{digest}.json"
        if cache_file.is_file():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            source.records = [Record(**{**r, "source": name}) for r in data["records"]]
            source.errors, source.notes = data["errors"], data["notes"]
            return source
    try:
        read_into(source, path)
    except Exception as exc:   # a broken file must not stop the other sources
        source.errors.append(f"could not read: {type(exc).__name__}: {exc}")
    if len(source.records) > MAX_RECORDS_PER_SOURCE:
        source.notes.append(f"only the first {MAX_RECORDS_PER_SOURCE} records were scanned")
        source.records = source.records[:MAX_RECORDS_PER_SOURCE]
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"records": [r.as_dict() for r in source.records],
                                          "errors": source.errors, "notes": source.notes}), encoding="utf-8")
    return source


def read_into(source: Source, path: Path) -> None:
    suffix = path.suffix.lower()
    if source.kind == "unsupported":
        hint = " (save old .xls files as .xlsx or .csv)" if suffix == ".xls" else ""
        source.errors.append(f"unsupported file type {suffix or 'without extension'}{hint}")
    elif suffix in (".csv", ".tsv"):
        read_csv_file(source, path)
    elif suffix in (".xlsx", ".xlsm"):
        read_xlsx(source, path)
    elif suffix == ".sql":
        read_sql_dump(source, path)
    elif source.kind == "database":
        read_sqlite(source, path)
    elif suffix == ".json":
        read_json(source, json.loads(path.read_text(encoding="utf-8-sig")))
    elif suffix in (".jsonl", ".ndjson"):
        for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if line.strip():
                read_json(source, json.loads(line), f"line {n}")
    elif suffix == ".xml":
        read_xml(source, path)
    elif suffix == ".pdf":
        read_pdf(source, path)
    elif suffix == ".docx":
        read_docx(source, path)
    elif suffix == ".pptx":
        read_pptx(source, path)
    elif suffix == ".eml":
        read_eml(source, path)
    elif source.kind == "image":
        read_image(source, path)
    elif suffix == ".zip":
        read_zip(source, path)
    else:
        read_text(source, path.read_text(encoding="utf-8", errors="replace"), html=suffix in (".html", ".htm"))


# ---------------------------------------------------------------------------
# Tables.

def table_rows(source: Source, rows: list[list[str]], container: str, method: str, place: str) -> None:
    """Turn a grid into records, using the first non-empty row as column names."""
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return
    header = [str(c).strip() or f"column {n + 1}" for n, c in enumerate(rows[0])]
    for n, row in enumerate(rows[1:], 2):
        values = {header[i] if i < len(header) else f"column {i + 1}": str(v).strip()
                  for i, v in enumerate(row) if str(v).strip()}
        if values:
            source.records.append(Record(source.name, f"{place}row {n}", container, values, method=method,
                                         observed_at=record_time(values)))


def read_csv_file(source: Source, path: Path) -> None:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        source.notes.append("decoded as Latin-1")
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    if delimiter == ",":
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;|\t").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    table_rows(source, rows, path.stem, "csv", "")


def xml_tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def column_index(cell_ref: str) -> int:
    letters = "".join(c for c in cell_ref if c.isalpha())
    number = 0
    for letter in letters.upper():
        number = number * 26 + (ord(letter) - 64)
    return number - 1


def read_xlsx(source: Source, path: Path) -> None:
    """Excel workbooks are zipped XML; read every sheet with the standard library."""
    with zipfile.ZipFile(path) as book:
        names = set(book.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for item in ElementTree.fromstring(book.read("xl/sharedStrings.xml")):
                if xml_tag(item) == "si":
                    shared.append("".join(t.text or "" for t in item.iter() if xml_tag(t) == "t"))
        targets = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for rel in ElementTree.fromstring(book.read("xl/_rels/workbook.xml.rels")):
                targets[rel.get("Id")] = rel.get("Target", "").lstrip("/").replace("xl/", "")
        sheets = []
        for element in ElementTree.fromstring(book.read("xl/workbook.xml")).iter():
            if xml_tag(element) == "sheet":
                rel_id = next((v for k, v in element.attrib.items() if k.endswith("}id")), "")
                sheets.append((element.get("name", "sheet"), "xl/" + targets.get(rel_id, "")))
        for sheet_name, member in sheets:
            if member not in names:
                source.errors.append(f"sheet {sheet_name} is missing from the workbook")
                continue
            grid: list[list[str]] = []
            for row in ElementTree.fromstring(book.read(member)).iter():
                if xml_tag(row) != "row":
                    continue
                values: list[str] = []
                for cell in row:
                    if xml_tag(cell) != "c":
                        continue
                    index = column_index(cell.get("r", "")) if cell.get("r") else len(values)
                    while len(values) < index:
                        values.append("")
                    kind = cell.get("t", "")
                    value = ""
                    for part in cell:
                        if xml_tag(part) == "v":
                            value = part.text or ""
                        elif xml_tag(part) == "is":
                            value = "".join(t.text or "" for t in part.iter() if xml_tag(t) == "t")
                    if kind == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    elif kind == "b":
                        value = "TRUE" if value == "1" else "FALSE"
                    values.append(value)
                grid.append(values)
            table_rows(source, grid, sheet_name, "xlsx", f"sheet {sheet_name}, ")


def read_sqlite(source: Source, path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        read_connection(source, conn, "sqlite")
    finally:
        conn.close()


def read_sql_dump(source: Source, path: Path) -> None:
    script = path.read_text(encoding="utf-8", errors="replace")
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(script)
        read_connection(source, conn, "sql")
    except sqlite3.Error as exc:
        # Other SQL dialects still contain readable INSERT lines.
        source.notes.append(f"not SQLite-compatible ({exc}); scanned as text lines")
        read_text(source, script)
    finally:
        conn.close()


def read_connection(source: Source, conn: sqlite3.Connection, method: str) -> None:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        cursor = conn.execute(f"SELECT * FROM {quoted}")
        columns = [d[0] for d in cursor.description]
        for n, row in enumerate(cursor, 1):
            values = {}
            for column, value in zip(columns, row):
                if value is None or value == "":
                    continue
                if isinstance(value, bytes):
                    continue
                text = str(value)
                if column == "record_json" and text.startswith("{"):
                    try:
                        values.update(flat_fields(json.loads(text)))
                        continue
                    except json.JSONDecodeError:
                        pass
                values[column] = text
            if values:
                source.records.append(Record(source.name, f"table {table}, row {n}", table, values,
                                             method=method, observed_at=record_time(values)))


# ---------------------------------------------------------------------------
# JSON and XML.

def flat_fields(obj: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Scalar fields of an object and its nested objects; lists become their own records."""
    out: dict[str, str] = {}
    for key, value in obj.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flat_fields(value, name + "."))
        elif isinstance(value, list):
            if value and all(not isinstance(v, (dict, list)) for v in value):
                out[name] = ", ".join(str(v) for v in value)
        elif value is not None and str(value) != "":
            out[name] = str(value)
    return out


def read_json(source: Source, obj: Any, place: str = "") -> None:
    def visit(value: Any, path: str, container: str) -> None:
        if isinstance(value, list):
            for n, child in enumerate(value):
                visit(child, f"{path}[{n}]", container)
        elif isinstance(value, dict):
            fields = flat_fields(value)
            if fields:
                source.records.append(Record(source.name, (place + " " + path).strip(), container, fields,
                                             method="json", observed_at=record_time(fields)))
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    visit_children(child, f"{path}.{key}", child_name(container, key))

    def visit_children(value: Any, path: str, container: str) -> None:
        # Nested objects are already part of their parent's fields; only lists of
        # objects, at any depth, start new records.
        if isinstance(value, list):
            for n, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}[{n}]", container)
        elif isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    visit_children(child, f"{path}.{key}", child_name(container, key))

    def child_name(container: str, key: str) -> str:
        # Keep the last two names, e.g. "account_service.profile_lookups".
        return ".".join((container.split(".")[-1:] if container else []) + [key])

    visit(obj, "$", Path(source.name).stem)


def read_xml(source: Source, path: Path) -> None:
    root = ElementTree.parse(path).getroot()
    for n, element in enumerate(root.iter()):
        children = list(element)
        fields = {xml_tag(c): (c.text or "").strip() for c in children if len(c) == 0 and (c.text or "").strip()}
        fields.update({k: v for k, v in element.attrib.items()})
        if fields and any(len(c) == 0 for c in children):
            source.records.append(Record(source.name, f"element {xml_tag(element)} #{n}", xml_tag(element),
                                         fields, method="xml", observed_at=record_time(fields)))


# ---------------------------------------------------------------------------
# Text, documents and images.

def read_text(source: Source, text: str, html: bool = False, method: str = "text", place: str = "",
              confidence: float = 1.0, container: str = "") -> None:
    if html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</tr>|</li>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = {}
        parsed = parse_line(line.strip()) if re.match(r"\d{4}-\d\d-\d\dT", line.strip()) else None
        if parsed:
            fields = {k: v for k, v in parsed.items() if v}
        source.records.append(Record(source.name, f"{place}line {n}", container, fields, text=line.strip(),
                                     method=method, observed_at=(parsed or {}).get("timestamp", ""),
                                     confidence=confidence))


def run_tool(command: list[str], timeout: int = 120) -> str:
    # Tesseract's own threading makes parallel runs very slow; one thread each is faster.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=timeout, env=env).stdout


def read_pdf(source: Source, path: Path) -> None:
    try:
        text = run_tool(["pdftotext", "-layout", str(path), "-"])
    except FileNotFoundError:
        source.errors.append("pdftotext is not installed (install poppler-utils)")
        return
    except subprocess.SubprocessError as exc:
        source.errors.append(f"pdftotext failed: {exc}")
        return
    pages = text.split("\f")
    if any(p.strip() for p in pages):
        for n, page in enumerate(pages, 1):
            read_text(source, page, method="pdf_text", place=f"page {n}, ")
        return
    # No text layer: a scanned document. Rasterize and OCR each page.
    try:
        with tempfile.TemporaryDirectory(prefix="ghosttrace-pdf-") as temp:
            run_tool(["pdftoppm", "-r", "200", "-png", str(path), str(Path(temp) / "page")])
            for n, page in enumerate(sorted(Path(temp).glob("page-*.png")), 1):
                for line, confidence in ocr_lines(page):
                    source.records.append(Record(source.name, f"page {n} (OCR)", "", {}, text=line,
                                                 method="pdf_ocr", confidence=confidence))
    except FileNotFoundError:
        source.errors.append("scanned PDF needs pdftoppm and tesseract for OCR")
    except subprocess.SubprocessError as exc:
        source.errors.append(f"PDF OCR failed: {exc}")
    if not source.records and not source.errors:
        source.errors.append("no text could be read from this PDF")


def ocr_lines(path: Path) -> list[tuple[str, float]]:
    """OCR text grouped by line, with the mean word confidence of each line."""
    tsv = run_tool(["tesseract", str(path), "stdout", "tsv"], timeout=180)
    lines: dict[tuple[str, str, str, str], list[tuple[str, float]]] = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        word = (row.get("text") or "").strip()
        if not word:
            continue
        key = (row["page_num"], row["block_num"], row["par_num"], row["line_num"])
        lines.setdefault(key, []).append((word, max(float(row.get("conf") or 0), 0.0) / 100))
    out = []
    for key in sorted(lines, key=lambda k: tuple(int(x) for x in k)):
        words = lines[key]
        out.append((" ".join(w for w, _ in words), sum(c for _, c in words) / len(words)))
    return out


def read_image(source: Source, path: Path) -> None:
    try:
        lines = ocr_lines(path)
    except FileNotFoundError:
        source.errors.append("tesseract is not installed (install tesseract-ocr)")
        return
    except subprocess.SubprocessError as exc:
        source.errors.append(f"OCR failed: {exc}")
        return
    for n, (line, confidence) in enumerate(lines, 1):
        source.records.append(Record(source.name, f"OCR line {n}", "", {}, text=line, method="ocr",
                                     confidence=confidence))
    if not lines:
        source.errors.append("OCR found no text in this image")
    else:
        mean = sum(c for _, c in lines) / len(lines)
        source.notes.append(f"OCR mean confidence {mean:.0%}; OCR can misread characters")


def read_docx(source: Source, path: Path) -> None:
    with zipfile.ZipFile(path) as doc:
        root = ElementTree.fromstring(doc.read("word/document.xml"))
    body = next((e for e in root if xml_tag(e) == "body"), root)
    paragraph = 0
    for n, element in enumerate(body):
        if xml_tag(element) == "p":
            text = "".join(t.text or "" for t in element.iter() if xml_tag(t) == "t").strip()
            paragraph += 1
            if text:
                source.records.append(Record(source.name, f"paragraph {paragraph}", "", {}, text=text,
                                             method="docx"))
        elif xml_tag(element) == "tbl":
            grid = []
            for row in element.iter():
                if xml_tag(row) == "tr":
                    grid.append(["".join(t.text or "" for t in cell.iter() if xml_tag(t) == "t")
                                 for cell in row if xml_tag(cell) == "tc"])
            table_rows(source, grid, f"table {n + 1}", "docx", f"table {n + 1}, ")


def read_pptx(source: Source, path: Path) -> None:
    with zipfile.ZipFile(path) as deck:
        slides = sorted((n for n in deck.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for name in slides:
            number = re.findall(r"\d+", name)[-1]
            root = ElementTree.fromstring(deck.read(name))
            for p, paragraph in enumerate((e for e in root.iter() if xml_tag(e) == "p"), 1):
                text = "".join(t.text or "" for t in paragraph.iter() if xml_tag(t) == "t").strip()
                if text:
                    source.records.append(Record(source.name, f"slide {number}, paragraph {p}", "", {},
                                                 text=text, method="pptx"))


def read_eml(source: Source, path: Path) -> None:
    message = BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())
    headers = {k.lower(): str(v) for k, v in message.items() if k.lower() in
               ("from", "to", "cc", "bcc", "reply-to", "subject", "date")}
    source.records.append(Record(source.name, "headers", "email", headers, method="eml"))
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        read_text(source, body.get_content(), html=body.get_content_type() == "text/html", method="eml",
                  place="body ")
    for part in message.iter_attachments():
        source.notes.append(f"attachment {part.get_filename()} not opened; save and add it separately")


def read_zip(source: Source, path: Path) -> None:
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(prefix="ghosttrace-zip-") as temp:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > MAX_FILE_BYTES:
                continue
            target = Path(temp) / Path(info.filename).name
            target.write_bytes(archive.read(info))
            member = read_source(target, name=f"{source.name}/{info.filename}")
            source.records.extend(member.records)
            source.errors.extend(f"{info.filename}: {e}" for e in member.errors)
            source.notes.extend(f"{info.filename}: {n}" for n in member.notes)
