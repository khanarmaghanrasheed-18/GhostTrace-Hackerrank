"""Independent checks for high-risk audit behavior; no public labels are used."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from ghosttrace.engine import AuditEngine, state  # noqa: E402
from ghosttrace.ingest import Observation  # noqa: E402


def observation(category: str, data: dict, captured_at: str = "2026-06-01T00:00:00Z") -> Observation:
    return Observation("evidence_test", "synthetic", "record:1", captured_at, captured_at,
                       "identity_service", "tombstone_9000", category, data, "json")


class CoreContractTests(unittest.TestCase):
    def test_later_capture_cannot_prove_earlier_state(self) -> None:
        later = observation("USER_PROFILE", {"resource_id": "account_9000"}, "2026-06-02T00:00:00Z")
        self.assertFalse(later.available_at("2026-06-01T23:59:59Z"))

    def test_tombstone_preserves_stable_reference_but_removes_direct_pii(self) -> None:
        terms = {"user_9000", "person@example.test"}
        exposed = observation("ACCOUNT_TOMBSTONE", {"resource_id": "tombstone_9000",
                                                      "customer_reference": "user_9000",
                                                      "email": "person@example.test"})
        redacted = observation("ACCOUNT_TOMBSTONE", {"resource_id": "tombstone_9000",
                                                       "customer_reference": "user_9000", "email": None})
        self.assertEqual(state(exposed, terms, set()), "EXPOSED")
        self.assertEqual(state(redacted, terms, set()), "REDACTED")

    def test_non_deletion_request_is_not_authorization(self) -> None:
        engine = AuditEngine(ROOT, use_gemini=False)
        self.assertEqual(engine.deletion_intent("Please export a copy of my records; do not delete my account.")[0], False)


if __name__ == "__main__":
    unittest.main()
