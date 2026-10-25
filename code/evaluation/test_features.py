"""Tests for the evidence graph, timeline and what-if analysis; no public answers are used.

Cases come from the participant dataset. The only synthetic part is
`StandInModel`, a clearly labelled stand-in for Gemini's response format, used to
check that model proposals are validated. It is never presented as a real call.
"""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from ghosttrace import whatif  # noqa: E402
from ghosttrace.engine import AuditEngine  # noqa: E402
from ghosttrace.ingest import read_csv  # noqa: E402
from ghosttrace.views import case_graph, case_timeline  # noqa: E402


class StandInModel:
    """SYNTHETIC stand-in returning Gemini-shaped proposals; not a model call."""
    ready = True
    model = "synthetic-stand-in"

    def __init__(self, answer: dict):
        self.answer = answer

    def propose_actions(self, question, resources, actions):
        return self.answer


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = AuditEngine(ROOT, use_gemini=False)
        rows = read_csv(ROOT / "dataset/sample_deletion_requests.csv") + read_csv(ROOT / "dataset/deletion_requests.csv")
        cls.requests = {r["case_id"]: r for r in rows}

    def model(self, case_id: str) -> whatif.CaseModel:
        return whatif.CaseModel(self.engine, self.requests[case_id])

    # -- evidence graph -----------------------------------------------------

    def test_every_edge_has_provenance_kind_and_time(self) -> None:
        for edge in self.engine.graph.edges:
            self.assertIn(edge.kind, {"observed", "claimed", "derived"})
            self.assertTrue(edge.evidence_id, edge)
            self.assertTrue(edge.event_time or edge.captured_at, edge)

    def test_graph_links_reproduce_the_engine_ownership(self) -> None:
        for user in list(self.engine.identity.current)[:200]:
            from_graph = {n.split(":", 1)[1] for n in self.engine.graph.links_to_person(user)}
            from_index = {r for r, owners in self.engine.owners.items() if user in owners}
            self.assertEqual(from_graph, from_index, user)

    def test_identity_traversal_follows_history_not_names(self) -> None:
        people = self.engine.graph.people_for("noah.f438085@outlook.com")   # an old address
        self.assertEqual(set(people), {"user_29"})
        self.assertEqual(people["user_29"][0].relation, "HAD_IDENTIFIER")
        self.assertIn("noah.f4380@outlook.com", self.engine.graph.identifier_terms("user_29"))
        kinds = {d.get("field") for d in self.engine.graph.nodes.values() if d["type"] == "identifier"}
        self.assertEqual(kinds, {"email", "phone", "email_hash"})   # no name or look-alike nodes

    def test_case_graph_uses_nothing_captured_after_audit(self) -> None:
        view = case_graph(self.engine, self.requests["case_29"])
        for record in view["records"]:
            for obs in record["observations"]:
                self.assertLessEqual(obs["captured_at"], view["audit_at"])
        self.assertGreater(view["excluded"]["edges_after_audit_at"], 0)

    def test_supported_recreation_path_is_found(self) -> None:
        paths = case_graph(self.engine, self.requests["case_29"])["recreation_paths"]
        flow = next(p for p in paths if p["process"] == "data_flow_3827")
        self.assertEqual(flow["state"], "ACTIVE")
        self.assertEqual(flow["destination_systems"], ["analytics_platform"])
        self.assertEqual(flow["destination_basis"], "derived from connector name")
        self.assertIn("analytics_5208", flow["can_recreate"])

    # -- timeline -----------------------------------------------------------

    def test_timeline_is_ordered_and_keeps_capture_time_separate(self) -> None:
        view = case_timeline(self.engine, self.requests["case_29"])
        times = [e["time"] for e in view["entries"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(e["time"] <= view["audit_at"] for e in view["entries"]))
        observed = [e for e in view["entries"] if e["type"] == "observed state"]
        self.assertTrue(observed and all(e["captured_at"] for e in observed))
        self.assertGreater(view["hidden_after_audit_at"], 0)
        later = case_timeline(self.engine, self.requests["case_29"], include_later=True)
        self.assertTrue(any(e["after_audit_at"] for e in later["entries"]))

    # -- what-if ------------------------------------------------------------

    def test_cleanup_without_closing_the_flow_leaves_a_recreation_risk(self) -> None:
        actions = [{"action": "DELETE_RECORD", "resource_id": "search_1993"},
                   {"action": "ANONYMIZE_RECORD", "resource_id": "analytics_5208"},
                   {"action": "REDACT_RECORD", "resource_id": "support_5281"}]
        result = whatif.assess(self.model("case_29"), actions)
        self.assertEqual(result["hypothetical_status"], "INCOMPLETE")
        self.assertEqual(result["recreation_paths"][0]["would_recreate_changed_records"], ["analytics_5208"])
        self.assertTrue(result["order_warnings"])

    def test_combined_proposal_resolves_the_modelled_issues(self) -> None:
        actions = [{"action": "DISABLE_DATA_FLOW", "resource_id": "data_flow_3827"},
                   {"action": "DELETE_RECORD", "resource_id": "search_1993"},
                   {"action": "ANONYMIZE_RECORD", "resource_id": "analytics_5208"},
                   {"action": "REDACT_RECORD", "resource_id": "support_5281"}]
        result = whatif.assess(self.model("case_29"), actions)
        self.assertEqual(result["hypothetical_status"], "COMPLETE")
        self.assertEqual(result["recreation_paths"], [])
        self.assertEqual(result["order_warnings"], [])

    def test_deleting_retained_records_is_a_policy_conflict(self) -> None:
        actions = [{"action": "DELETE_RECORD", "resource_id": "backup_3245"},
                   {"action": "DELETE_RECORD", "resource_id": "audit_record_1797"}]
        result = whatif.assess(self.model("case_17"), actions)
        conflicts = {e["resource_id"] for e in result["outcome"]["policy conflict"]}
        self.assertEqual(conflicts, {"backup_3245", "audit_record_1797"})
        self.assertEqual(result["hypothetical_status"], "INCOMPLETE")
        blocked = whatif.assess(self.model("case_17"), [{"action": "BLOCK_RESTORE", "resource_id": "backup_3245"}])
        self.assertEqual(blocked["hypothetical_status"], "COMPLETE")

    def test_weaker_treatment_leaves_the_obligation(self) -> None:
        result = whatif.assess(self.model("case_29"), [{"action": "REDACT_RECORD", "resource_id": "search_1993"}])
        self.assertIn("search_1993", {e["resource_id"] for e in result["outcome"]["still required"]})

    def test_ambiguous_target_requires_clarification(self) -> None:
        answer = whatif.interpret(self.model("case_33"), "What if I delete the export?")
        self.assertEqual(answer["accepted"], [])
        self.assertEqual(answer["clarify"][0]["candidates"], ["export_1260", "export_1842"])
        self.assertTrue(answer["interpreter"].startswith("local-rules"))

    def test_unsupported_and_nonexistent_targets_are_rejected(self) -> None:
        model = self.model("case_33")
        self.assertEqual(whatif.validate(model, "DELETE_RECORD", "search_99999")["status"], "rejected")
        self.assertIn("not linked to this person", whatif.validate(model, "DELETE_RECORD", "search_428")["reason"])
        self.assertEqual(whatif.validate(model, "DISABLE_DATA_FLOW", "export_1260")["status"], "rejected")
        self.assertEqual(whatif.validate(model, "SHRED_RECORD", "export_1260")["status"], "rejected")
        missing = whatif.interpret(model, "block the backup restore")
        self.assertIn("no backup record", missing["rejected"][0]["reason"])

    def test_model_proposals_are_validated_like_any_other(self) -> None:
        stand_in = StandInModel({"actions": [
            {"action": "DELETE_RECORD", "resource_id": "export_1842"},
            {"action": "DELETE_RECORD", "resource_id": "export_4242"},          # invented ID
            {"action": "DELETE_RECORD", "resource_id": None, "candidates": ["export_1260", "export_1842"]},
            {"action": "OVERWRITE_RECORD", "resource_id": "export_1260"}]})     # unsupported action
        answer = whatif.interpret(self.model("case_33"), "delete my exports", stand_in)
        self.assertEqual(answer["interpreter"], "gemini:synthetic-stand-in")
        self.assertEqual([a["resource_id"] for a in answer["accepted"]], ["export_1842"])
        self.assertEqual(len(answer["rejected"]), 2)
        self.assertEqual(len(answer["clarify"]), 1)

    def test_original_audit_result_is_unchanged(self) -> None:
        request = self.requests["case_29"]
        before = copy.deepcopy(self.engine.investigate(request))
        model = whatif.CaseModel(self.engine, request)
        whatif.assess(model, [{"action": "DELETE_RECORD", "resource_id": "audit_record_3954"},
                              {"action": "DISABLE_DATA_FLOW", "resource_id": "data_flow_3827"}])
        self.assertEqual(self.engine.investigate(request), before)
        self.assertEqual(model.audit_row, before[0])

    def test_scenarios_accumulate_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = whatif.add_actions([], [{"action": "DELETE_RECORD", "resource_id": "search_1993"}])
            whatif.save_scenario(root, "case_29", first)
            more = whatif.add_actions(whatif.load_scenario(root, "case_29"),
                                      [{"action": "DISABLE_DATA_FLOW", "resource_id": "data_flow_3827"}])
            whatif.save_scenario(root, "case_29", more)
            self.assertEqual(len(whatif.load_scenario(root, "case_29")), 2)
            whatif.reset_scenario(root, "case_29")
            self.assertEqual(whatif.load_scenario(root, "case_29"), [])


if __name__ == "__main__":
    unittest.main()
