"""Regression tests for the audit engine; no public sample answers are used.

Each test names the bug category it guards. Synthetic inputs are used where a
rule can be tested in isolation; the rest run the engine on participant data and
check properties of its reasoning, never expected sample rows.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from ghosttrace.engine import AuditEngine, deletion_intent, judge, order_steps  # noqa: E402
from ghosttrace.ingest import (Observation, make_record, parse_line, read_csv,  # noqa: E402
                               text_records)
from ghosttrace.validate import validate_rows  # noqa: E402


def observation(resource: str, state: str, when: str, store: str = "primary",
                identifying: bool | None = None, evidence: str = "evidence_test") -> Observation:
    return Observation(evidence, "synthetic", "record:1", when, when, "test_service", resource, store,
                       "", state, identifying)


def request(case_id: str, email: str, text: str, requested: str, reported: str, audit: str,
            status: str = "COMPLETE") -> dict[str, str]:
    return {"case_id": case_id, "email": email, "requested_at": requested, "reported_status": status,
            "reported_at": reported, "audit_at": audit, "request_text": text}


class IntentTests(unittest.TestCase):
    """Bug category: non-deletion requests were treated as authorized."""

    def test_declined_or_scoped_requests_are_not_deletion(self) -> None:
        for text in ("Please stop the promotional emails. I still need the account, so I am not asking for deletion.",
                     "Please remove me from marketing lists only. I still want receipts.",
                     "Can you tell me what personal information you hold? This is not a request to erase it.",
                     "My surname is misspelled. Can you correct it without removing any of my records?",
                     "Please keep the account as it is and send me a copy of my data."):
            self.assertIs(deletion_intent(text)[0], False, text)

    def test_deletion_in_other_languages_is_authorized(self) -> None:
        for text in ("Je demande la suppression définitive du compte.", "Solicito la eliminación permanente de la cuenta.",
                     "Solicito a exclusão permanente da conta.", "アカウントの完全な削除を依頼します。",
                     "계정의 영구 삭제를 요청합니다.", "Please close my account and delete my personal data."):
            self.assertIs(deletion_intent(text)[0], True, text)


class ExtractionTests(unittest.TestCase):
    """Bug category: extraction and normalization of evidence."""

    def test_every_supplied_log_syntax_yields_service_event_and_resource(self) -> None:
        lines = [
            '2026-01-01T00:00:00Z level=INFO service=account_service trace_id=t1 resource_id=account_1 event=profile_lookup_completed rows=0',
            '2026-01-01T00:00:00Z [INFO] [search_service] [t2] index_query_completed: resource_id="search_2" result="hit"',
            '2026-01-01T00:00:00Z | INFO | security_service | t3 | audit_record_read_completed | resource_id="audit_record_3" rows="1"',
            '2026-01-01T00:00:00Z <15>1 node-1 support_platform t4 - - level=INFO resource_id="support_4" event="message_read"',
            '2026-01-01T00:00:00Z requestId=t5 level=INFO function=analytics_platform message="resource_id=\\"analytics_5\\" event=\\"analytics_profile_read\\""',
            '2026-01-01T00:00:00Z {"service":"integration_service","trace_id":"t6","resource_id":"data_flow_6","event":"connector_heartbeat","enabled":"true"}',
        ]
        for line in lines:
            data = parse_line(line)
            self.assertTrue(data and data.get("service") and data.get("event") and data.get("resource_id"), line)

    def test_lookup_key_on_trace_start_links_the_completed_line(self) -> None:
        text = ("2026-01-01T00:00:00Z level=DEBUG service=search_service trace_id=t9 event=index_query_started match_key=\"a@b.test\"\n"
                "2026-01-01T00:00:01Z level=INFO service=search_service trace_id=t9 resource_id=search_9 event=index_query_completed result=hit\n")
        records = text_records(text)
        self.assertEqual(len(records), 1)
        self.assertIn("a@b.test", records[0]["keys"])
        self.assertEqual(records[0]["state"], "PRESENT")

    def test_identifiers_inside_nested_documents_are_kept(self) -> None:
        record = make_record("search_1", "row", {"resource_id": "search_1",
                                                 "document": {"email": "x@y.test", "customer_reference": "user_1"}})
        self.assertIn("x@y.test", record["keys"])
        self.assertTrue(record["identifying"])

    def test_names_show_a_person_but_do_not_link_records(self) -> None:
        record = make_record("support_1", "row", {"requester_name": "Ana Silva", "requester_email": None})
        self.assertTrue(record["identifying"])
        self.assertNotIn("ana silva", record["keys"])

    def test_contact_index_replica_is_a_secondary_identifier(self) -> None:
        text = ("2026-01-01T00:00:00Z level=DEBUG service=support_replica trace_id=r1 event=contact_query_started lookup_key=a@b.test source=legacy_contact_index\n"
                "2026-01-01T00:00:01Z level=INFO service=support_replica trace_id=r1 event=contact_query_completed resource_id=support_7 rows=1\n")
        self.assertEqual(text_records(text)[0]["category"], "SECONDARY_IDENTIFIER")


class PolicyTests(unittest.TestCase):
    """Bug categories: policy interpretation, retention and tombstones."""

    def test_tombstone_keeps_stable_reference_but_not_direct_identifiers(self) -> None:
        exposed = make_record("tombstone_1", "row", {"subject_reference": "user_1", "email": "p@x.test"})
        redacted = make_record("tombstone_1", "row", {"subject_reference": "user_1", "email": None, "name": None})
        self.assertEqual(judge(observation("tombstone_1", "PRESENT", "t", identifying=exposed["identifying"]), "REDACTED"),
                         "violation")
        self.assertEqual(judge(observation("tombstone_1", "PRESENT", "t", identifying=redacted["identifying"]), "REDACTED"),
                         "ok")

    def test_analytics_with_stable_reference_is_still_attributable(self) -> None:
        record = make_record("analytics_1", "row", {"customer_reference": "user_1", "email": None})
        self.assertTrue(record["identifying"])

    def test_retained_record_is_verified_never_deleted(self) -> None:
        self.assertEqual(judge(observation("audit_record_1", "PRESENT", "t"), "PRESENT"), "ok")
        self.assertEqual(judge(observation("audit_record_1", "ABSENT", "t"), "PRESENT"), "unknown")

    def test_flow_shutdown_and_restore_block_come_before_record_changes(self) -> None:
        steps = [AuditEngine._step("REMEDIATION", "search_service", "search_1", "DELETE_RECORD", ""),
                 AuditEngine._step("REMEDIATION", "analytics_platform", "analytics_1", "ANONYMIZE_RECORD", ""),
                 AuditEngine._step("REMEDIATION", "backup_service", "backup_1", "BLOCK_RESTORE", ""),
                 AuditEngine._step("FINDING", "integration_service", "data_flow_1", "REPORT_ACTIVE_DATA_FLOW", "e"),
                 AuditEngine._step("REMEDIATION", "integration_service", "data_flow_1", "DISABLE_DATA_FLOW", "")]
        ordered = [s["operation"] for s in order_steps(steps)]
        self.assertEqual(ordered, ["REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
                                   "DELETE_RECORD", "ANONYMIZE_RECORD"])
        self.assertEqual([s["step_no"] for s in order_steps(steps)], [1, 2, 3, 4, 5])


class EngineTests(unittest.TestCase):
    """Engine behaviour on participant data: identity, time, conflicts and gaps."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = AuditEngine(ROOT, use_gemini=False)
        cls.samples = {r["case_id"]: r for r in read_csv(ROOT / "dataset/sample_deletion_requests.csv")}
        cls.requests = {r["case_id"]: r for r in read_csv(ROOT / "dataset/deletion_requests.csv")}
        times = {r["file_id"]: r["captured_at"] for r in read_csv(ROOT / "dataset/evidence_files.csv")}
        times.update({r["activity_id"]: r["event_time"] for r in read_csv(ROOT / "dataset/system_activity.csv")})
        cls.times = times

    def test_historical_email_resolves_to_the_same_person(self) -> None:
        # user_29 changed email in the audit log; both addresses identify them.
        old, _ = self.engine.identity.resolve("noah.f438085@outlook.com", "2026-08-27T12:00:00Z")
        new, _ = self.engine.identity.resolve("NOAH.F4380@OUTLOOK.COM", "2026-08-27T12:00:00Z")
        self.assertEqual(old, "user_29")
        self.assertEqual(new, "user_29")
        self.assertIn("noah.f438085@outlook.com", self.engine.identity.terms("user_29"))

    def test_no_case_cites_evidence_captured_after_its_audit(self) -> None:
        cases = list(self.samples.values()) + list(self.requests.values())
        rows = [self.engine.investigate(r)[0] for r in cases]
        cutoffs = {r["case_id"]: r["audit_at"] for r in cases}
        errors = validate_rows(sorted(rows, key=lambda r: r["case_id"]), sorted(cutoffs), set(self.times),
                               self.times, cutoffs)
        self.assertEqual(errors, [])

    def test_resource_first_seen_after_audit_is_out_of_scope(self) -> None:
        # export_1842 first appears in a July statement; the June request cannot include it.
        _, detail = self.engine.investigate(self.requests["case_52"])
        self.assertNotIn("export_1842", [r["resource_id"] for r in detail["resources"]])
        self.assertIn("export_1842", detail.get("later_resources", []))

    def test_earlier_deletion_cycle_is_not_reaudited(self) -> None:
        # user_17 was audited in March; the March account is not part of the August request.
        _, detail = self.engine.investigate(self.samples["case_17"])
        self.assertIn("account_6523", detail.get("earlier_cycle_resources", []))

    def test_replica_copy_conflicting_with_primary_is_reported(self) -> None:
        primary_gone = observation("account_9", "ABSENT", "2026-02-01T00:00:00Z", "primary")
        replica_left = observation("account_9", "PRESENT", "2026-01-01T00:00:00Z", "failover_replica", True)
        states = self.engine.store_states("account_9", [primary_gone, replica_left], "2026-03-01T00:00:00Z",
                                          set(), "USER_PROFILE")
        verdicts = {store: judge(obs, "ABSENT") for store, obs in states.items()}
        self.assertEqual(verdicts["primary"], "ok")
        self.assertEqual(verdicts["failover_replica"], "violation")

    def test_later_observation_supersedes_earlier_in_same_store(self) -> None:
        earlier = observation("search_9", "PRESENT", "2026-01-01T00:00:00Z", identifying=True)
        later = observation("search_9", "ABSENT", "2026-02-01T00:00:00Z")
        future = observation("search_9", "PRESENT", "2026-04-01T00:00:00Z", identifying=True)
        available = [o for o in (earlier, later, future) if o.available_at("2026-03-01T00:00:00Z")]
        states = self.engine.store_states("search_9", available, "2026-03-01T00:00:00Z", set(), "SEARCH_INDEX_ENTRY")
        self.assertEqual(states["primary"].state, "ABSENT")

    def test_missing_inventory_row_is_not_proof_of_absence(self) -> None:
        # Inventories are partial extracts: only account profiles may be inferred absent.
        export = self.engine.store_states("export_1260", [], "2026-07-19T13:00:00Z", set(), "USER_EXPORT")
        audit = self.engine.store_states("audit_record_9", [], "2026-09-30T00:00:00Z", set(), "SECURITY_AUDIT_LOG")
        self.assertEqual(export, {})
        self.assertEqual(audit, {})

    def test_unknown_person_gets_no_unsupported_verdict(self) -> None:
        row, detail = self.engine.investigate(request(
            "case_test1", "nobody@example.test", "Please delete my account.",
            "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z"))
        self.assertEqual(row["deletion_status"], "INCOMPLETE")
        self.assertEqual(json.loads(row["steps_json"]), [])
        self.assertTrue(detail["uncertain"])

    def test_authorized_request_with_no_supported_action_is_not_complete(self) -> None:
        # A real person with no evidence before an early cutoff: nothing can be proved.
        linked_users = {u for owners in self.engine.owners.values() for u in owners}
        user = next(u for u in sorted(self.engine.identity.current) if u not in linked_users)
        email = self.engine.identity.current[user]["email"]
        row, detail = self.engine.investigate(request(
            "case_test2", email, "Please delete my account.",
            "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"))
        self.assertEqual(row["deletion_status"], "INCOMPLETE")
        self.assertEqual(json.loads(row["steps_json"]), [])
        self.assertIn("no evidence before audit_at establishes any resource state", detail["uncertain"])

    def test_workflow_attribution_respects_reported_run_outcome(self) -> None:
        results = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
        events = {e["activity_id"]: e for e in self.engine.activity}
        every = {**self.samples, **self.requests}
        for case, resources in self.engine.assigned.items():
            for activity_id in resources.values():
                event = events[activity_id]
                if "submitted primary profile" in event["details"]:
                    self.assertEqual(results[every[case]["reported_status"]], event["reported_result"], case)

    def test_user_reference_alias_needs_a_single_person_document(self) -> None:
        # The legal register lists six user references; it must not count as the
        # user_17 tombstone, which only the one-person registry screenshot shows.
        sources = {o.evidence_id for o in self.engine.by_resource["tombstone_17"]}
        self.assertNotIn("evidence_27", sources)
        self.assertIn("evidence_42", sources)


if __name__ == "__main__":
    unittest.main()
