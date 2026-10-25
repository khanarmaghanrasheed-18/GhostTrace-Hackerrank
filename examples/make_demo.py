"""Build the fictional "Acme Outdoor" demo workspace in examples/acme_demo/.

Every person, address and company here is invented; emails use the reserved
example.com/.net/.org domains and phones the UK drama range 07700 900xxx.

The files cover each supported format and the situations GhostTrace looks for:
a soft-deleted CRM row, an old email kept in an identity-change log, a search
document under the old address, analytics still keyed by the customer ID, an
active marketing sync, a restorable backup, a tombstone that still holds an email
hash, a newsletter list, a mail log, a zipped export, a complaint letter, an
uploaded identity scan, an admin-console screenshot, an invoice that must be
kept, and security events inside their retention period. A second person with
the same name shows why names alone are only possible matches.

Run:  python3 examples/make_demo.py   (needs Pillow only for the two image files)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import zipfile
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "acme_demo"
SUBJECT_EMAIL = "d.okafor@example.com"
OLD_EMAIL = "daniel.okafor@example.net"
PHONE = "+44 7700 900123"
CUSTOMER_ID = "C-10442"


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def write_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> None:
    """A minimal valid Excel workbook, written with the standard library."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        names = list(sheets)
        book.writestr("[Content_Types].xml",
                      '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                      '<Default Extension="xml" ContentType="application/xml"/>'
                      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                      + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                                for i in range(len(names))) + "</Types>")
        book.writestr("_rels/.rels",
                      '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book.writestr("xl/workbook.xml",
                      '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                      + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                      + "</sheets></workbook>")
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                                for i in range(len(names))) + "</Relationships>")
        for i, name in enumerate(names):
            rows = []
            for r, row in enumerate(sheets[name], 1):
                cells = "".join(f'<c r="{column_letter(c)}{r}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
                                for c, v in enumerate(row))
                rows.append(f'<row r="{r}">{cells}</row>')
            book.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                          '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                          f'<sheetData>{"".join(rows)}</sheetData></worksheet>')


def write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as doc:
        doc.writestr("[Content_Types].xml",
                     '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        doc.writestr("_rels/.rels",
                     '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        doc.writestr("word/document.xml",
                     '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                     f"<w:body>{body}</w:body></w:document>")


def write_text_pdf(path: Path, lines: list[str]) -> None:
    """A one-page PDF with a real text layer."""
    def pdf_text(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = "BT /F1 12 Tf 56 780 Td 16 TL " + " ".join(f"({pdf_text(line)}) Tj T*" for line in lines) + " ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(out.getvalue())


def write_images(folder: Path) -> list[str]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Pillow is not installed: skipping the screenshot and the scanned PDF.")
        return []

    def font(size: int):
        for name in ("DejaVuSans.ttf", "Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    shot = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(shot)
    draw.rectangle([0, 0, 1280, 64], fill=(30, 41, 59))
    draw.text((24, 18), "Acme Outdoor  Customer Admin", fill="white", font=font(26))
    draw.rectangle([0, 64, 240, 720], fill=(241, 245, 249))
    for i, item in enumerate(["Customers", "Orders", "Support", "Reports"]):
        draw.text((24, 100 + i * 44), item, fill=(51, 65, 85), font=font(22))
    rows = [("Customer", "Daniel Okafor"), ("Customer ID", CUSTOMER_ID), ("Email", SUBJECT_EMAIL),
            ("Phone", PHONE), ("Status", "Active"), ("Last order", "ORD-88231  14 Aug 2026")]
    draw.text((280, 96), "Customer profile", fill=(15, 23, 42), font=font(30))
    for i, (label, value) in enumerate(rows):
        draw.text((280, 160 + i * 52), label, fill=(100, 116, 139), font=font(22))
        draw.text((520, 160 + i * 52), value, fill=(15, 23, 42), font=font(24))
    shot.save(folder / "customer_admin_console.png")

    scan = Image.new("RGB", (1240, 1754), (250, 249, 245))
    draw = ImageDraw.Draw(scan)
    lines = ["ACME OUTDOOR - SUPPORT UPLOAD", "", "IDENTITY DOCUMENT (customer upload)", "",
             "Name: DANIEL OKAFOR", f"Customer: {CUSTOMER_ID}", f"Email: {OLD_EMAIL}",
             "Document: passport copy, page 1", "Uploaded: 03 Feb 2026 for a delivery dispute"]
    for i, line in enumerate(lines):
        draw.text((120, 160 + i * 70), line, fill=(20, 20, 20), font=font(40))
    scan.save(folder / "uploaded_identity_scan.pdf", "PDF", resolution=150)
    return ["customer_admin_console.png", "uploaded_identity_scan.pdf"]


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.iterdir():
        if old.is_file():
            old.unlink()
    files: dict[str, str] = {}

    write_xlsx(OUT / "crm_customers.xlsx", {
        "Customers": [["customer_id", "full_name", "email", "phone", "city", "status", "updated_at"],
                      [CUSTOMER_ID, "Daniel Okafor", SUBJECT_EMAIL, PHONE, "Leeds", "deleted", "2026-09-01T09:30:00Z"],
                      ["C-20991", "Daniel Okafor", "dokafor.design@example.org", "+44 7700 900877", "Bristol", "active", "2026-08-20T12:00:00Z"],
                      ["C-11873", "Priya Shah", "p.shah@example.org", "+44 7700 900456", "York", "active", "2026-07-11T08:00:00Z"],
                      ["C-30215", "Mei Lin", "mei.lin@example.com", "+44 7700 900789", "Bath", "active", "2026-06-02T16:45:00Z"]],
        "Notes": [["customer_id", "note", "author_email"],
                  [CUSTOMER_ID, "Asked for account closure on 28 Aug 2026", "agent.kim@example.com"],
                  ["C-11873", "Prefers SMS", "agent.kim@example.com"]]})
    files["crm_customers.xlsx"] = "2026-09-20T08:00:00Z"

    write_csv(OUT / "identity_changes.csv", ["user_id", "field_name", "old_value", "new_value", "event_time"],
              [[CUSTOMER_ID, "email", OLD_EMAIL, SUBJECT_EMAIL, "2025-11-02T10:15:00Z"],
               ["C-11873", "phone", "+44 7700 900111", "+44 7700 900456", "2026-01-15T09:00:00Z"]])
    files["identity_changes.csv"] = "2026-09-20T08:00:00Z"

    (OUT / "support_tickets.json").write_text(json.dumps({"exported_at": "2026-09-19T17:00:00Z", "tickets": [
        {"ticket_id": "T-5521", "customer_email": OLD_EMAIL, "agent_email": "agent.kim@example.com",
         "subject": "Parcel not delivered", "updated_at": "2026-02-04T11:20:00Z",
         "messages": [{"from": "customer", "text": f"My order ORD-77102 has not arrived. Call me on {PHONE}."},
                      {"from": "agent", "text": "Sorry about that, a replacement is on its way."}]},
        {"ticket_id": "T-6610", "customer_email": "p.shah@example.org", "agent_email": "agent.kim@example.com",
         "subject": "Invoice copy", "updated_at": "2026-08-01T10:00:00Z",
         "messages": [{"from": "customer", "text": "Could you resend invoice 2025-117?"}]}]}, indent=2), encoding="utf-8")
    files["support_tickets.json"] = "2026-09-19T17:00:00Z"

    db = OUT / "app_production.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE users (user_id TEXT PRIMARY KEY, email TEXT, phone TEXT, status TEXT, deleted_at TEXT);
        CREATE TABLE deleted_users (user_id TEXT PRIMARY KEY, email_hash TEXT, deleted_at TEXT);
        CREATE TABLE search_documents (doc_id TEXT PRIMARY KEY, display_name TEXT, email TEXT, indexed_at TEXT);
        CREATE TABLE analytics_events (event_id TEXT PRIMARY KEY, user_ref TEXT, event TEXT, ts TEXT);
        CREATE TABLE connectors (connector_id TEXT PRIMARY KEY, connector_name TEXT, customer_email TEXT,
                                 destination TEXT, enabled INTEGER, updated_at TEXT);
        CREATE TABLE backups (backup_id TEXT PRIMARY KEY, customer_reference TEXT, archive_path TEXT,
                              restore_allowed INTEGER, created_at TEXT);
    """)
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?)", [
        (CUSTOMER_ID, None, None, "deleted", "2026-09-01T09:30:00Z"),
        ("C-11873", "p.shah@example.org", "+44 7700 900456", "active", None)])
    conn.execute("INSERT INTO deleted_users VALUES (?,?,?)",
                 (CUSTOMER_ID, hashlib.sha256(SUBJECT_EMAIL.encode()).hexdigest(), "2026-09-01T09:30:00Z"))
    conn.executemany("INSERT INTO search_documents VALUES (?,?,?,?)", [
        ("doc-4410", "Daniel Okafor", OLD_EMAIL, "2026-03-03T02:00:00Z"),
        ("doc-5120", "Priya Shah", "p.shah@example.org", "2026-07-12T02:00:00Z")])
    conn.executemany("INSERT INTO analytics_events VALUES (?,?,?,?)", [
        ("ev-1", CUSTOMER_ID, "product_viewed", "2026-08-30T19:00:00Z"),
        ("ev-2", "anon-77", "product_viewed", "2026-08-30T19:05:00Z")])
    conn.execute("INSERT INTO connectors VALUES (?,?,?,?,?,?)",
                 ("conn-12", "crm_to_marketing_audience_sync", SUBJECT_EMAIL, "marketing audience", 1, "2026-09-18T06:00:00Z"))
    conn.execute("INSERT INTO backups VALUES (?,?,?,?,?)",
                 ("bk-2026-08", CUSTOMER_ID, "s3://acme-backups/crm/2026-08.tar.zst", 1, "2026-08-31T23:00:00Z"))
    conn.commit()
    conn.close()
    files["app_production.db"] = "2026-09-20T07:00:00Z"

    write_csv(OUT / "newsletter_subscribers.csv", ["email", "name", "subscribed_at", "list"],
              [[OLD_EMAIL, "Daniel O.", "2024-05-01T00:00:00Z", "weekly deals"],
               ["mei.lin@example.com", "Mei", "2025-02-10T00:00:00Z", "weekly deals"]])
    files["newsletter_subscribers.csv"] = "2026-09-15T00:00:00Z"

    (OUT / "mail_delivery.log").write_text("\n".join([
        f"2026-08-14T10:02:11Z level=INFO service=mailer event=sent to={OLD_EMAIL} template=order_shipped",
        "2026-08-14T10:02:12Z level=INFO service=mailer event=sent to=p.shah@example.org template=order_shipped",
        f"2026-08-29T08:00:00Z level=INFO service=mailer event=sent to={SUBJECT_EMAIL} template=closure_confirmation",
    ]) + "\n", encoding="utf-8")
    files["mail_delivery.log"] = "2026-09-20T00:00:00Z"

    write_csv(OUT / "security_audit.csv", ["event_id", "user_id", "event", "recorded_at", "retention_until"],
              [["sec-901", CUSTOMER_ID, "account_closure_requested", "2026-08-28T14:00:00Z", "2033-08-28T00:00:00Z"],
               ["sec-902", CUSTOMER_ID, "account_closed", "2026-09-01T09:30:00Z", "2033-09-01T00:00:00Z"]])
    files["security_audit.csv"] = "2026-09-20T08:00:00Z"

    export = io.StringIO()
    csv.writer(export).writerows([["customer_id", "full_name", "email", "phone"], [CUSTOMER_ID, "Daniel Okafor", OLD_EMAIL, PHONE]])
    with zipfile.ZipFile(OUT / "customer_export_2026-03.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("customer_export_2026-03/export_customers.csv", export.getvalue())
    files["customer_export_2026-03.zip"] = "2026-03-31T18:00:00Z"

    write_docx(OUT / "complaint_letter.docx", [
        "Complaint about a late delivery", "From: Daniel Okafor", f"Email: {OLD_EMAIL}",
        "My order ORD-77102 arrived three weeks late. I would like a refund of the shipping cost.",
        "Handled by: agent.kim@example.com"])
    files["complaint_letter.docx"] = "2026-02-05T09:00:00Z"

    write_text_pdf(OUT / "invoice_2025_118.pdf", [
        "ACME OUTDOOR LTD - INVOICE 2025-118", "Date: 14 Aug 2026", "Bill to: Daniel Okafor",
        "14 Park Row, Leeds LS1 5HD", f"Customer: {CUSTOMER_ID}", f"Email: {SUBJECT_EMAIL}",
        "1 x Trail tent 2P   GBP 189.00", "VAT 20%   GBP 37.80", "Total   GBP 226.80"])
    files["invoice_2025_118.pdf"] = "2026-08-14T10:00:00Z"

    for name in write_images(OUT):
        files[name] = "2026-09-19T12:00:00Z" if name.endswith(".png") else "2026-02-03T15:00:00Z"

    write_csv(OUT.parent / "acme_manifest.csv", ["file_path", "captured_at"], [[k, v] for k, v in sorted(files.items())])
    write_csv(OUT.parent / "acme_requests.csv", ["request_id", "email", "phone", "name", "request_text"], [
        ["REQ-1", SUBJECT_EMAIL, "", "Daniel Okafor", "Please delete my account and all my personal data."],
        ["REQ-2", "p.shah@example.org", "", "", "Please confirm whether you still hold my data."],
        ["REQ-3", "mei.lin@example.com", "", "", "Erase everything you have about me."],
        ["REQ-4", "tom.becker@example.com", "", "", "Please update my delivery address; keep my account active."],
        ["REQ-5", "nobody@example.com", "", "", "Delete my data."]])
    print(f"Wrote {len(files)} files to {OUT}")


if __name__ == "__main__":
    build()
