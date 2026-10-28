"""Decide what kind of data a record holds and what state it is in.

The category drives the policy rule. It comes from, in order: an explicit
record_type field, a user's choice for the whole source, an opt-in field, and
finally keywords in the source, sheet or table name and column names. The
first keyword list that matches wins, so specific kinds come before general ones.
"""

from __future__ import annotations

import csv
from pathlib import Path
import re
from typing import Any


POLICY_FILE = Path(__file__).parent / "policy" / "default_policy.csv"

CATEGORY_WORDS = [
    ("ACCOUNT_TOMBSTONE", r"tombstone|deleted_(?:users?|customers?|accounts?)|deleted_customer_references"),
    ("BACKUP_ARCHIVE", r"backup|archive|restore|vault"),
    ("DATA_FLOW", r"connector|integration|pipeline|webhook|data_flow|etl|subscriptions?\b|sync"),
    ("SECURITY_AUDIT_LOG", r"audit|security|access_log|login|auth_?log|sentinel"),
    ("LEGAL_HOLD_RECORD", r"legal_?hold|litigation|retention_register|legal"),
    ("FINANCIAL_RECORD", r"invoice|billing|payment|receipt|tax|transaction|statement|ledger|order"),
    ("SUPPORT_ATTACHMENT", r"attachment|upload|scan|identity_document"),
    ("SUPPORT_MESSAGE", r"support|ticket|helpdesk|help_desk|conversation|chat|message|complaint|inbox"),
    ("EMAIL_DELIVERY_LOG", r"email_?log|mail_?log|delivery|smtp|notification|sent_mail"),
    ("MARKETING_PROFILE", r"marketing|campaign|newsletter|mailing|audience|lead|subscriber|opt_?in"),
    ("SEARCH_INDEX_ENTRY", r"search|index|elastic|solr"),
    ("PRODUCT_TELEMETRY", r"telemetry|usage_events|clickstream"),
    ("ANALYTICS_PROFILE", r"analytics|metrics|tracking|segment|cohort|events"),
    ("CACHE_ENTRY", r"cache|redis|memcache"),
    ("USER_EXPORT", r"export|download|takeout"),
    ("SECONDARY_IDENTIFIER", r"lookup|alias|contact_index|identifier_map"),
    ("USER_PROFILE", r"customer|user|account|profile|member|client|people|person|contact|employee|patient"),
]
CATEGORIES = [name for name, _ in CATEGORY_WORDS] + ["PERSONAL_RECORD"]

EMAIL_COLUMN = re.compile(r"e-?mail", re.I)
PHONE_COLUMN = re.compile(r"phone|mobile|\btel\b|telephone|msisdn", re.I)
ID_COLUMN = re.compile(r"(?:^|_|\b)(?:user|customer|client|member|subject|person|account|patient|employee)"
                       r"_?(?:id|ref|reference|number|no)$|^uid$|^customer_reference$|^subject_reference$", re.I)
NAME_COLUMN = re.compile(r"(?:^|_)(?:full_?name|display_?name|first_?name|last_?name|surname|name)$", re.I)
SUBJECT_PREFIX = re.compile(r"^(?:customer|user|requester|owner|subject|client|member|account|contact|recipient|"
                            r"billing|profile)_", re.I)
REDACTED_VALUE = re.compile(r"^\s*(?:\[?redacted\]?|\*{3,}|x{3,}|<removed>|removed|anonymi[sz]ed|null|none|n/?a|-)\s*$",
                            re.I)
TRUE_WORDS = {"true", "1", "yes", "y", "enabled", "active", "on", "running", "allowed", "allow"}
FALSE_WORDS = {"false", "0", "no", "n", "disabled", "inactive", "off", "stopped", "paused", "blocked", "denied"}


def load_policy(path: Path | None = None) -> dict[str, dict[str, str]]:
    """data_category -> rule row. A custom file replaces matching categories only."""
    rules: dict[str, dict[str, str]] = {}
    for file in [POLICY_FILE] + ([path] if path else []):
        with file.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("data_category") and row.get("required_action"):
                    rules[row["data_category"].strip().upper()] = {k: (v or "").strip() for k, v in row.items()}
    return rules


def column_kind(column: str) -> str:
    """email, phone, id or name when a column holds a person identifier."""
    name = column.split(".")[-1]
    if EMAIL_COLUMN.search(name) and not re.search(r"opt|status|verified|sent|count", name, re.I):
        return "email"
    if PHONE_COLUMN.search(name):
        return "phone"
    if ID_COLUMN.search(name):
        return "id"
    if NAME_COLUMN.search(name) and not re.search(r"connector|file|index|table|product|event|company", name, re.I):
        return "name"
    return ""


def belongs_to_subject(column: str) -> bool:
    """Columns such as customer_email describe the person; agent_email does not."""
    name = column.split(".")[-1]
    return bool(SUBJECT_PREFIX.match(name)) or name.lower() in {"email", "phone", "mobile", "name", "full_name",
                                                                  "display_name", "user_id", "customer_id"}


def category_for(record: Any, source_category: str = "") -> tuple[str, str]:
    """(category, reason) for a record."""
    fields = record.fields
    stated = fields.get("record_type", "").strip().upper()
    if stated in CATEGORIES:
        return stated, "record_type field"
    if source_category:
        return source_category, "chosen for the whole source"
    if fields.get("email_opt_in") or fields.get("marketing_opt_in"):
        return "MARKETING_PROFILE", "opt-in field"
    lowered = {k.lower() for k in fields}
    if {"old_value", "new_value"} & lowered and {"field_name", "field", "attribute"} & lowered:
        return "SECONDARY_IDENTIFIER", "identity change history: it maps old identifiers to the person"
    places = [record.container, Path(record.source).stem, fields.get("collection_name", ""),
              fields.get("collection", ""), fields.get("data_store", "")]
    for place in places:
        text = re.sub(r"[\s\-.]+", "_", place.lower())
        for category, words in CATEGORY_WORDS:
            if text and re.search(words, text):
                return category, f"name '{place}'"
    columns = " ".join(fields).lower()
    for category, words in CATEGORY_WORDS:
        if columns and re.search(words, columns):
            return category, "column names"
    return "PERSONAL_RECORD", "no more specific kind recognized"


def truth(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    return None


def field_value(fields: dict[str, str], *names: str) -> str:
    lowered = {k.lower().split(".")[-1]: v for k, v in fields.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    return ""


def record_state(record: Any, category: str) -> tuple[str, str]:
    """(state, reason). States: PRESENT, ABSENT, ACTIVE, DISABLED, RESTORABLE, BLOCKED."""
    fields = record.fields
    text = record.text.lower()
    if category == "DATA_FLOW":
        flag = field_value(fields, "enabled", "active", "is_active", "status", "state", "mode")
        if truth(flag) is not None:
            return ("ACTIVE", f"enabled flag '{flag}'") if truth(flag) else ("DISABLED", f"flag '{flag}'")
        if field_value(fields, "last_delivery_status").lower() == "stopped":
            return "DISABLED", "last delivery stopped"
        if re.search(r"\b(continuous|enabled|running|healthy)\b", text):
            return "ACTIVE", "page shows the connector running"
        if re.search(r"\b(disabled|paused|stopped)\b", text):
            return "DISABLED", "page shows the connector stopped"
        return "ACTIVE", "no disabled flag found; treated as able to copy data"
    if category == "BACKUP_ARCHIVE":
        flag = field_value(fields, "restore_allowed", "restorable", "restore_access", "restore_enabled")
        if truth(flag) is not None:
            return ("RESTORABLE", f"restore flag '{flag}'") if truth(flag) else ("BLOCKED", f"restore flag '{flag}'")
        if field_value(fields, "eligible") and truth(field_value(fields, "deletion_block")) is not True:
            return ("RESTORABLE", "restore candidate") if truth(field_value(fields, "eligible")) else ("BLOCKED", "not eligible")
        if re.search(r"restore (blocked|denied|disabled)|legal block", text):
            return "BLOCKED", "page shows restore blocked"
        return "RESTORABLE", "no restore block found; an archive holding the data can bring it back"
    result = field_value(fields, "result", "lookup_result", "status_code", "http_status").upper()
    if result in {"NOT_FOUND", "ABSENT", "404", "NO_MATCH", "NO_HITS", "MISS"} or field_value(fields, "rows") == "0":
        return "ABSENT", f"lookup reports {result or 'zero rows'}"
    if field_value(fields, "total_hits", "hits.total") == "0":
        return "ABSENT", "search returned no hits"
    if category == "ACCOUNT_TOMBSTONE":
        return "PRESENT", "deleted-account reference is kept"
    status = field_value(fields, "account_status", "status", "state").lower()
    if status in {"deleted", "removed", "erased", "purged"} or truth(field_value(fields, "is_deleted", "deleted")):
        return "PRESENT", "marked deleted, but the row and its values are still stored (soft delete)"
    if field_value(fields, "deleted_at"):
        return "PRESENT", "has deleted_at, but the row and its values are still stored (soft delete)"
    return "PRESENT", "record is stored"


def identifying_values(record: Any) -> list[tuple[str, str]]:
    """(column, value) pairs that still show who the person is."""
    found = []
    for column, value in record.fields.items():
        kind = column_kind(column)
        if kind in ("email", "phone", "name") and value and not REDACTED_VALUE.match(value):
            found.append((column, value))
    return found
