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

Tables, SQL, JSON Lines and text are read row by row, so a file of any size can
be read without holding it in memory. Formats that must be parsed whole (JSON,
XML, HTML, Office files, PDFs, email, images) are limited to MAX_FILE_BYTES.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any
from xml.etree import ElementTree
import zipfile

from .ingest import parse_line

MAX_FILE_BYTES = 300 * 1024 * 1024   # only for formats that are parsed whole
csv.field_size_limit(2 ** 31 - 1)    # very long cells (notes, JSON blobs) are still read

SPREADSHEET = {".csv", ".tsv", ".xlsx", ".xlsm"}
DATABASE = {".db", ".sqlite", ".sqlite3", ".sql"}
STRUCTURED = {".json", ".jsonl", ".ndjson", ".xml"}
DOCUMENT = {".pdf", ".docx", ".pptx", ".eml", ".txt", ".md", ".html", ".htm", ".log", ".text"}
IMAGE = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
ARCHIVE = {".zip"}
SUPPORTED = SPREADSHEET | DATABASE | STRUCTURED | DOCUMENT | IMAGE | ARCHIVE
WHOLE_FILE = {".json", ".xml", ".html", ".htm", ".pdf", ".docx", ".pptx", ".eml"} | IMAGE
TIME_FIELDS = ("updated_at", "modified_at", "last_modified", "observed_at", "captured_at", "recorded_at",
               "event_time", "timestamp", "time", "date", "created_at")
ISO_TIME = re.compile(r"\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?)?")
FORM_FIELD = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 ./#()_-]{0,40}?)\s*[:=]\s*(\S.*?)\s*$")
# Screens often show a label column without colons: "Customer ID    C-10442".
FORM_LABEL = re.compile(r"^\s*(email address|e-?mail|phone number|phone|mobile|telephone|customer (?:id|number|no)|"
                        r"user (?:id|name)|account (?:id|number|no|status)|member (?:id|number)|client id|"
                        r"full name|name|status)\s+(\S.*?)\s*$", re.I)


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
    records: list[Record] = field(default_factory=list)   # kept only when there is no sink
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    count: int = 0
    containers: list[str] = field(default_factory=list)
    sink: Any = None      # receives each record instead of the list (the on-disk index)
    digest: str = ""      # set when the source is in the index
    store: Any = None

    def add(self, record: Record) -> None:
        self.count += 1
        if record.container and record.container not in self.containers:
            self.containers.append(record.container)
        if self.sink:
            self.sink(record)
        else:
            self.records.append(record)

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "size": self.size, "captured_at": self.captured_at,
                "records": self.count, "containers": sorted(self.containers), "errors": self.errors,
                "notes": self.notes, "readable": not self.errors or self.count > 0}


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
    if path.is_file():
        with path.open("rb") as handle:
            if handle.read(16).startswith(b"SQLite format 3"):
                return "database"
    return "unsupported"


# ---------------------------------------------------------------------------
# Entry points. read_source keeps the records in memory; the index (store.py)
# gives read_into a sink instead, so large files never sit in memory.

def read_source(path: Path, name: str | None = None, captured_at: str = "") -> Source:
    source = Source(name or path.name, kind_of(path), path.stat().st_size, captured_at or file_time(path))
    try:
        read_into(source, path)
    except Exception as exc:   # a broken file must not stop the other sources
        source.errors.append(f"could not read: {type(exc).__name__}: {exc}")
    return source


def read_into(source: Source, path: Path) -> None:
    suffix = path.suffix.lower()
    if source.kind == "unsupported":
        hint = " (save old .xls files as .xlsx or .csv)" if suffix == ".xls" else ""
        source.errors.append(f"unsupported file type {suffix or 'without extension'}{hint}")
    elif suffix in WHOLE_FILE and path.stat().st_size > MAX_FILE_BYTES:
        hint = " (save large JSON as JSON Lines, one object per line)" if suffix == ".json" else "; split it first"
        source.errors.append(f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB{hint}")
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
        with path.open(encoding="utf-8-sig") as handle:
            for n, line in enumerate(handle, 1):
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
    elif suffix in (".html", ".htm"):
        read_text(source, path.read_text(encoding="utf-8", errors="replace"), html=True)
    else:
        with path.open(encoding="utf-8", errors="replace") as handle:
            read_lines(source, handle)


# ---------------------------------------------------------------------------
# Tables.

def table_rows(source: Source, rows: Any, container: str, method: str, place: str) -> None:
    """Turn rows into records, using the first non-empty row as column names.
    Rows are numbered among the non-empty rows, the header being row 1."""
    header: list[str] | None = None
    n = 0
    for row in rows:
        if not any(str(c).strip() for c in row):
            continue
        n += 1
        if header is None:
            header = [str(c).strip() or f"column {i + 1}" for i, c in enumerate(row)]
            continue
        values = {header[i] if i < len(header) else f"column {i + 1}": str(v).strip()
                  for i, v in enumerate(row) if str(v).strip()}
        if values:
            source.add(Record(source.name, f"{place}row {n}", container, values, method=method,
                              observed_at=record_time(values)))


def read_csv_file(source: Source, path: Path) -> None:
    encoding = "utf-8-sig"
    try:
        with path.open(encoding=encoding) as handle:
            while handle.read(1 << 20):
                pass
    except UnicodeDecodeError:
        encoding = "latin-1"
        source.notes.append("decoded as Latin-1")
    with path.open(newline="", encoding=encoding) as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        if delimiter == ",":
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;|\t").delimiter
            except csv.Error:
                delimiter = ","
        table_rows(source, csv.reader(handle, delimiter=delimiter), path.stem, "csv", "")


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
            with book.open(member) as handle:
                table_rows(source, sheet_rows(handle, shared), sheet_name, "xlsx", f"sheet {sheet_name}, ")


def sheet_rows(handle: Any, shared: list[str]) -> Any:
    """The rows of one worksheet, parsed as they are read so a large sheet fits in memory."""
    parent = None
    for event, element in ElementTree.iterparse(handle, events=("start", "end")):
        tag = xml_tag(element)
        if event == "start":
            if tag == "sheetData":
                parent = element
            continue
        if tag != "row":
            continue
        values: list[str] = []
        for cell in element:
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
        yield values
        if parent is not None:
            parent.clear()   # rows already read are dropped


def read_sqlite(source: Source, path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        read_connection(source, conn, "sqlite")
    finally:
        conn.close()


def read_sql_dump(source: Source, path: Path) -> None:
    """Load the dump into a temporary SQLite file statement by statement, then read its tables."""
    with tempfile.TemporaryDirectory(prefix="ghosttrace-sql-") as temp:
        conn = sqlite3.connect(Path(temp) / "dump.db", isolation_level=None)
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        try:
            batch, statement = [], ""
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    statement += line
                    if sqlite3.complete_statement(statement):
                        batch.append(statement)
                        statement = ""
                        if len(batch) >= 2000:
                            conn.executescript("".join(batch))
                            batch = []
            batch.append(statement)
            conn.executescript("".join(batch))
            read_connection(source, conn, "sql")
        except sqlite3.Error as exc:
            # Other SQL dialects still contain readable INSERT lines.
            source.notes.append(f"not SQLite-compatible ({exc}); scanned as text lines")
            with path.open(encoding="utf-8", errors="replace") as handle:
                read_lines(source, handle)
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
                source.add(Record(source.name, f"table {table}, row {n}", table, values,
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
                source.add(Record(source.name, (place + " " + path).strip(), container, fields,
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
            source.add(Record(source.name, f"element {xml_tag(element)} #{n}", xml_tag(element),
                              fields, method="xml", observed_at=record_time(fields)))


# ---------------------------------------------------------------------------
# Text, documents and images.

def read_text(source: Source, text: str, html: bool = False, method: str = "text", place: str = "",
              confidence: float = 1.0, container: str = "") -> None:
    if html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</tr>|</li>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    read_lines(source, text.splitlines(), method, place, confidence, container)


def read_lines(source: Source, lines: Any, method: str = "text", place: str = "", confidence: float = 1.0,
               container: str = "") -> None:
    """One record per non-empty line; log lines are parsed into fields."""
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        fields = {}
        parsed = parse_line(line) if re.match(r"\d{4}-\d\d-\d\dT", line) else None
        if parsed:
            fields = {k: v for k, v in parsed.items() if v}
        source.add(Record(source.name, f"{place}line {n}", container, fields, text=line,
                          method=method, observed_at=(parsed or {}).get("timestamp", ""), confidence=confidence))


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
                lines = ocr_lines(page)
                for line, confidence in lines:
                    source.add(Record(source.name, f"page {n} (OCR)", "", {}, text=line,
                                      method="pdf_ocr", confidence=confidence))
                fields, lowest = form_fields(lines)
                if fields:
                    source.add(Record(source.name, f"page {n} (OCR) form fields", "", fields,
                                      method="pdf_ocr", confidence=lowest))
    except FileNotFoundError:
        source.errors.append("scanned PDF needs pdftoppm and tesseract for OCR")
    except subprocess.SubprocessError as exc:
        source.errors.append(f"PDF OCR failed: {exc}")
    if not source.count and not source.errors:
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


def form_fields(lines: list[tuple[str, float]]) -> tuple[dict[str, str], float]:
    """The 'Label: value' lines of a screenshot as fields, e.g. 'Customer ID: C-2041'
    becomes customer_id=C-2041, with the lowest OCR confidence among those lines."""
    fields: dict[str, str] = {}
    lowest = 1.0
    for line, confidence in lines:
        found = FORM_FIELD.match(line) or FORM_LABEL.match(line)
        if not found:
            continue
        label = re.sub(r"[^a-z0-9]+", "_", found.group(1).lower().replace("e-mail", "email")).strip("_")
        if label and label not in fields:
            fields[label] = found.group(2)
            lowest = min(lowest, confidence)
    return fields, lowest


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
        source.add(Record(source.name, f"OCR line {n}", "", {}, text=line, method="ocr", confidence=confidence))
    fields, lowest = form_fields(lines)
    if fields:
        source.add(Record(source.name, "OCR form fields", "", fields, method="ocr", confidence=lowest))
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
                source.add(Record(source.name, f"paragraph {paragraph}", "", {}, text=text, method="docx"))
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
                    source.add(Record(source.name, f"slide {number}, paragraph {p}", "", {}, text=text, method="pptx"))


def read_eml(source: Source, path: Path) -> None:
    message = BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())
    headers = {k.lower(): str(v) for k, v in message.items() if k.lower() in
               ("from", "to", "cc", "bcc", "reply-to", "subject", "date")}
    source.add(Record(source.name, "headers", "email", headers, method="eml"))
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        read_text(source, body.get_content(), html=body.get_content_type() == "text/html", method="eml",
                  place="body ")
    for part in message.iter_attachments():
        source.notes.append(f"attachment {part.get_filename()} not opened; save and add it separately")


def read_zip(source: Source, path: Path) -> None:
    """Each member is read with the same rules; its records are placed as 'member :: row 2'."""
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(prefix="ghosttrace-zip-") as temp:
        for info in archive.infolist():
            if info.is_dir():
                continue
            target = Path(temp) / (Path(info.filename).name or "member")
            with archive.open(info) as packed, target.open("wb") as unpacked:
                shutil.copyfileobj(packed, unpacked)
            member = Source(f"{source.name}/{info.filename}", kind_of(target), info.file_size, "")
            member.sink = forward_to(source, info.filename)
            try:
                read_into(member, target)
            except Exception as exc:
                member.errors.append(f"could not read: {type(exc).__name__}: {exc}")
            source.errors.extend(f"{info.filename}: {e}" for e in member.errors)
            source.notes.extend(f"{info.filename}: {n}" for n in member.notes)
            target.unlink()


def forward_to(source: Source, member: str) -> Any:
    def add(record: Record) -> None:
        record.source = source.name
        record.location = f"{member} :: {record.location}"
        source.add(record)
    return add
