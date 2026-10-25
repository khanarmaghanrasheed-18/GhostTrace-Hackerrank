"""Current and historical identifiers for every person in the identity tables."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .ingest import iso_at, normalize, read_csv


IDENTIFIER_FIELDS = ("email", "phone", "email_hash")


class IdentityIndex:
    def __init__(self, root: Path):
        self.current = {r["user_id"]: r for r in read_csv(root / "dataset" / "user_identities.csv")}
        self.history = read_csv(root / "dataset" / "user_audit_log.csv")
        # identifier -> users who held it, with the audit rows that prove historical use
        self.owners: dict[str, set[str]] = defaultdict(set)
        self.sources: dict[tuple[str, str], list[str]] = defaultdict(list)
        for user_id, row in self.current.items():
            self.owners[normalize(user_id)].add(user_id)
            for name in IDENTIFIER_FIELDS:
                value = normalize(row.get(name))
                if value:
                    self.owners[value].add(user_id)
                    self.sources[(user_id, value)].append("user_identities.csv")
        for row in self.history:
            if row["field_name"] not in IDENTIFIER_FIELDS:
                continue
            for key in ("old_value", "new_value"):
                value = normalize(row[key])
                if value:
                    self.owners[value].add(row["user_id"])
                    self.sources[(row["user_id"], value)].append(row["audit_id"])

    def users_for(self, value: str) -> set[str]:
        return set(self.owners.get(normalize(value), set()))

    def resolve(self, email: str, requested_at: str) -> tuple[str | None, list[str]]:
        """Return the single person the request email identifies, or None if ambiguous."""
        candidates = sorted(self.users_for(email))
        if len(candidates) <= 1:
            return (candidates[0] if candidates else None), candidates
        # Prefer the person who held this address when the request was made.
        holders = [u for u in candidates if self._held_at(u, normalize(email), iso_at(requested_at))]
        if len(holders) == 1:
            return holders[0], candidates
        return None, candidates

    def _held_at(self, user_id: str, value: str, when: str) -> bool:
        changes = sorted((r for r in self.history if r["user_id"] == user_id and r["field_name"] == "email"),
                         key=lambda r: r["event_time"])
        held = normalize(self.current.get(user_id, {}).get("email")) == value
        for row in reversed(changes):
            if iso_at(row["event_time"]) > when:
                held = normalize(row["old_value"]) == value
            else:
                break
        return held

    def terms(self, user_id: str) -> set[str]:
        """Every identifier the person has used, with the stable user reference."""
        found = {normalize(user_id)}
        row = self.current.get(user_id, {})
        for name in IDENTIFIER_FIELDS:
            if normalize(row.get(name)):
                found.add(normalize(row[name]))
        for audit in self.history:
            if audit["user_id"] == user_id and audit["field_name"] in IDENTIFIER_FIELDS:
                for key in ("old_value", "new_value"):
                    if normalize(audit[key]):
                        found.add(normalize(audit[key]))
        return found

    def identity_evidence(self, user_id: str) -> list[dict[str, str]]:
        """Historical identifier rows, for inspection output."""
        return [{"audit_id": r["audit_id"], "event_time": r["event_time"], "field_name": r["field_name"],
                 "old_value": r["old_value"], "new_value": r["new_value"]}
                for r in self.history if r["user_id"] == user_id and r["field_name"] in IDENTIFIER_FIELDS]
