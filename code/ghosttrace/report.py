"""A printable report of one check: a single self-contained HTML page.

It is meant to be saved, printed to PDF, or attached to a deletion ticket. It has
no scripts and loads nothing from outside; every value from the files is escaped.
"""

from __future__ import annotations

from html import escape
from typing import Any

VERDICT_CLASS = {"STILL_EXISTS": "bad", "NEEDS_REVIEW": "warn", "DELETED": "good", "NO_TRACE": "good"}
VERDICT_ADVICE = {
    "STILL_EXISTS": "The person's data is still held in at least one place below, or a process can bring it back. "
                    "Follow the cleanup plan, then run the check again on fresh exports.",
    "NEEDS_REVIEW": "Nothing confirmed remains, but some items need a person to look at them: possible matches, "
                    "unknown states, a form's claim or files that could not be read.",
    "DELETED": "Every copy found has the treatment the policy requires. Records the policy keeps are listed as compliant.",
    "NO_TRACE": "Nothing about this person was found in the readable sources.",
}
STYLE = """
:root { --ink: #16181d; --muted: #5d6472; --line: #dfe3ea; --bad: #b42318; --bad-bg: #fdecea; --warn: #8a5a00;
        --warn-bg: #fff4dc; --good: #1d7a45; --good-bg: #e7f6ed; }
* { box-sizing: border-box; }
body { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--ink); margin: 0; background: #fff; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 28px 48px; }
header { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 2px solid var(--ink);
         padding-bottom: 10px; margin-bottom: 20px; }
header .brand { font-weight: 700; letter-spacing: .02em; }
header .meta { color: var(--muted); font-size: 12px; text-align: right; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 28px 0 8px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.verdict { border-radius: 10px; padding: 14px 18px; margin: 16px 0; }
.verdict strong { font-size: 18px; display: block; }
.bad { background: var(--bad-bg); color: var(--bad); } .warn { background: var(--warn-bg); color: var(--warn); }
.good { background: var(--good-bg); color: var(--good); }
.counts { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin: 12px 0; }
.counts div { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
.counts b { display: block; font-size: 20px; }
.counts span { color: var(--muted); font-size: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { text-align: left; vertical-align: top; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; }
td.wrap { word-break: break-word; }
.pill { display: inline-block; border-radius: 999px; padding: 0 8px; font-size: 11.5px; font-weight: 600; }
.muted { color: var(--muted); } .small { font-size: 12px; }
footer { margin-top: 32px; padding-top: 10px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
@media print { main { padding: 0; } h2 { break-after: avoid; } tr { break-inside: avoid; } }
"""
LABEL = {
    "USER_PROFILE": "Customer profile", "SECONDARY_IDENTIFIER": "Identifier map", "SEARCH_INDEX_ENTRY": "Search index entry",
    "CACHE_ENTRY": "Cache entry", "SUPPORT_MESSAGE": "Support conversation", "SUPPORT_ATTACHMENT": "Support attachment",
    "ANALYTICS_PROFILE": "Analytics", "PRODUCT_TELEMETRY": "Product telemetry", "USER_EXPORT": "Data export",
    "MARKETING_PROFILE": "Marketing list", "EMAIL_DELIVERY_LOG": "Email log", "SECURITY_AUDIT_LOG": "Security log",
    "LEGAL_HOLD_RECORD": "Legal hold", "BACKUP_ARCHIVE": "Backup", "ACCOUNT_TOMBSTONE": "Deleted-account reference",
    "FINANCIAL_RECORD": "Invoice / financial record", "PERSONAL_RECORD": "Other personal record",
    "DATA_FLOW": "Sync / integration", "FORM_LOOKUP": "Search screen",
}
NEEDS = {"ABSENT": "delete it", "REDACTED": "redact it", "ANONYMIZED": "anonymize it", "PRESENT": "keep it",
         "DISABLED": "turn it off", "BLOCKED": "block restoring"}


def label(category: str) -> str:
    return LABEL.get(category, category.replace("_", " ").lower())


PILL = {"violation": "bad", "compliant": "good", "possible match": "warn", "unknown": "warn", "claim": "warn"}


def cell(value: Any) -> str:
    return f"<td class=\"wrap\">{escape(str(value if value is not None else ''))}</td>"


def table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "<p class=\"muted\">None.</p>"
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(cell(v) for v in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render(saved: dict[str, Any]) -> str:
    r, req = saved["result"], saved["request"]
    who = ", ".join(req.get("emails", []) + req.get("phones", []) + req.get("ids", []) + req.get("names", []))
    c = r["counts"]
    current = [f for f in r["findings"] if f["verdict"] != "history"]
    remaining = [f for f in current if f["verdict"] == "violation"]
    counts = "".join(f"<div><b>{escape(str(v))}</b><span>{escape(k)}</span></div>" for k, v in (
        ("still exist", c["still_exists"]), ("compliant", c["compliant"]), ("need review", c["needs_review"]),
        ("recreation paths", len(r["recreation_paths"])), ("records scanned", f"{c['records_scanned']:,}")))
    findings_rows = []
    for f in current:
        pill = f"<span class=\"pill {PILL.get(f['verdict'], '')}\">{escape(f['verdict'])}</span>"
        findings_rows.append(f"<tr><td>{escape(f['id'])}</td><td>{pill}</td>{cell(label(f['category']))}"
                             f"{cell(f['source'] + ' / ' + f['location'])}{cell(f['state'] + ': ' + f['state_reason'])}"
                             f"{cell((f['rule_id'] + ' ' + f['policy']).strip())}"
                             f"{cell(', '.join(m['identifier'].split(':', 1)[-1] for m in f['matches']))}</tr>")
    findings = (table(["ID", "Verdict", "Kind of data", "Where", "State", "Policy", "Matched"], []) if not findings_rows else
                "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in ("ID", "Verdict", "Kind of data", "Where", "State",
                                                                         "Policy", "Matched"))
                + "</tr></thead><tbody>" + "".join(findings_rows) + "</tbody></table>")
    unlisted = (f"<p class=\"small muted\">{c['possible_not_listed']:,} more possible matches (name only or a shared "
                "phone) are not listed.</p>") if c.get("possible_not_listed") else ""
    parts = [
        f"<header><span class=\"brand\">GhostTrace &middot; deletion check</span>"
        f"<span class=\"meta\">Check {escape(saved['id'])}<br>Run {escape(saved['created_at'])}</span></header>",
        f"<h1>{escape(who or 'Check')}</h1>",
        f"<p class=\"muted\">{escape(r['label'])}"
        + (f" State judged as of {escape(r['as_of'])}." if r.get("as_of") else "")
        + (f" Reference: {escape(req['label'])}." if req.get("label") else "") + "</p>",
        f"<div class=\"verdict {VERDICT_CLASS.get(r['status'], '')}\"><strong>{escape(r['status_text'])}</strong>"
        f"{escape(VERDICT_ADVICE.get(r['status'], ''))}</div>",
        f"<div class=\"counts\">{counts}</div>",
        "<h2>Where the data still exists</h2>",
        table(["ID", "Kind of data", "Where", "Policy needs", "Why"],
              [[f["id"], label(f["category"]), f"{f['source']} / {f['location']}",
                f"{f['rule_id']}: {NEEDS.get(f['required_state'], f['required_state'])}", f["state_reason"]] for f in remaining]),
        "<h2>Processes that can bring data back</h2>",
        table(["Process", "Kind", "State", "Where", "Can bring back", "Basis"],
              [[p["process"], p["kind"], p["state"], p["where"], ", ".join(p["can_recreate"]) or "—", p["basis"]]
               for p in r["recreation_paths"]]),
        "<h2>Cleanup plan</h2>",
        table(["#", "Phase", "Operation", "Target state", "Item", "Where", "Proof"],
              [[s["step_no"], s["phase"], s["operation"], s["state"], s["resource_id"], s.get("where", s["system_id"]),
                s["evidence_id"] or "—"] for s in r["plan"]]),
        "<h2>Identifiers used</h2>",
        table(["Kind", "Value", "Origin", "How it is known", "Found in"],
              [[i["kind"], i["value"], i["origin"], i["how"], i["evidence"]] for i in r["subject"]]),
        "<h2>All findings</h2>", findings, unlisted,
        "<h2>Coverage</h2>",
        ("<p class=\"bad small\">Not readable, so not checked: " + escape(", ".join(r["coverage"]["unreadable"])) + "</p>")
        if r["coverage"]["unreadable"] else "",
        table(["Source", "Kind", "Records", "Captured"],
              [[s["name"], s["kind"], f"{s['records']:,}", s["captured_at"]] for s in r["coverage"]["sources"]]),
        "<footer>GhostTrace checks only the files it was given, on this computer. A clean result means nothing "
        "was found in those files; it does not prove that no copy exists elsewhere. Policy rules used: "
        + escape(", ".join(f"{p['rule_id']} {label(p['data_category'])}: {p['required_action'].lower()}" for p in r["policy"]))
        + ".</footer>",
    ]
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>GhostTrace report {escape(saved['id'])}</title><style>{STYLE}</style></head>"
            f"<body><main>{''.join(parts)}</main></body></html>")
