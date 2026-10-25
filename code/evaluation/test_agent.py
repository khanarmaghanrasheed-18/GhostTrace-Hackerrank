"""Tests for the general GhostTrace agent (any files, any person).

Inputs are synthetic files written by these tests, or the fictional Acme
Outdoor demo in examples/. No real personal data is used.
"""

from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "examples"))

import make_demo  # noqa: E402
from ghosttrace import scanner  # noqa: E402
from ghosttrace.readers import read_source  # noqa: E402

DEMO = ROOT / "examples" / "acme_demo"
HAVE_OCR = shutil.which("tesseract") is not None and shutil.which("pdftotext") is not None


def scan(folder: Path, as_of: str = "", meta: dict | None = None, policy: Path | None = None, **who) -> dict:
    sources = scanner.load_sources([folder], None, meta or {}, base=folder)
    subject = scanner.Subject(who.get("emails", []), who.get("phones", []), who.get("names", []), who.get("ids", []),
                              who.get("confirmed", []), who.get("rejected", []))
    return scanner.run_check(sources, scanner.Check(subject, as_of, policy, meta or {}))


def by_source(result: dict, name: str) -> list[dict]:
    return [f for f in result["findings"] if f["source"].startswith(name)]


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ghosttrace-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_each_supported_format_yields_records(self) -> None:
        paths = [
            self.write("a.csv", "email;name\nx@example.com;X\n"),                      # sniffed delimiter
            self.write("b.tsv", "email\tname\nx@example.com\tX\n"),
            self.write("c.jsonl", '{"email": "x@example.com"}\n{"email": "y@example.com"}\n'),
            self.write("d.xml", "<people><person><email>x@example.com</email></person></people>"),
            self.write("e.html", "<html><body><p>Contact x@example.com</p><script>var a=1</script></body></html>"),
            self.write("f.eml", "From: x@example.com\nTo: shop@example.org\nSubject: hi\n\nPlease delete me.\n"),
            self.write("g.sql", "CREATE TABLE users (id TEXT, email TEXT);\nINSERT INTO users VALUES ('1','x@example.com');\n"),
        ]
        make_demo.write_xlsx(self.dir / "h.xlsx", {"People": [["email", "phone"], ["x@example.com", "+44 7700 900001"]]})
        make_demo.write_docx(self.dir / "i.docx", ["Letter from x@example.com"])
        with zipfile.ZipFile(self.dir / "j.pptx", "w") as deck:
            deck.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>x@example.com</a:t></a:r></a:p></p:sld>')
        with zipfile.ZipFile(self.dir / "k.zip", "w") as archive:
            archive.writestr("inner/people.csv", "email\nx@example.com\n")
        conn = sqlite3.connect(self.dir / "l.db")
        conn.execute("CREATE TABLE t (email TEXT)")
        conn.execute("INSERT INTO t VALUES ('x@example.com')")
        conn.commit()
        conn.close()
        for path in paths + [self.dir / n for n in ("h.xlsx", "i.docx", "j.pptx", "k.zip", "l.db")]:
            source = read_source(path)
            self.assertFalse(source.errors, (path.name, source.errors))
            texts = json.dumps([r.as_dict() for r in source.records])
            self.assertIn("x@example.com", texts, path.name)
        self.assertNotIn("var a", json.dumps([r.text for r in read_source(self.dir / "e.html").records]))

    def test_nested_json_lists_at_any_depth_are_read(self) -> None:
        path = self.write("n.json", json.dumps({"exports": {"crm": {"rows": [{"email": "x@example.com"}]}}}))
        records = read_source(path).records
        self.assertTrue(any(r.fields.get("email") == "x@example.com" and r.container == "crm.rows" for r in records))

    def test_other_sql_dialects_fall_back_to_text(self) -> None:
        path = self.write("m.sql", "CREATE TABLE `u` (`id` int AUTO_INCREMENT) ENGINE=InnoDB;\nINSERT INTO `u` VALUES (1,'x@example.com');\n")
        source = read_source(path)
        self.assertTrue(any("scanned as text" in n for n in source.notes))
        self.assertTrue(any("x@example.com" in r.text for r in source.records))

    def test_unreadable_files_are_reported_not_ignored(self) -> None:
        self.assertIn(".xlsx", read_source(self.write("old.xls", "binary")).errors[0])   # tells how to fix it
        broken = self.dir / "broken.xlsx"
        broken.write_bytes(b"not a zip")
        self.assertTrue(read_source(broken).errors)
        self.write("x.csv", "email\nx@example.com\n")
        result = scan(self.dir, emails=["nobody@example.com"])
        self.assertEqual(result["status"], "NEEDS_REVIEW")   # unreadable files block a clean verdict
        self.assertIn("broken.xlsx", result["coverage"]["unreadable"])

    @unittest.skipUnless(HAVE_OCR, "needs pdftotext and tesseract")
    def test_pdf_text_scanned_pdf_and_screenshot(self) -> None:
        self.assertEqual(read_source(DEMO / "invoice_2025_118.pdf").records[0].method, "pdf_text")
        scanned = read_source(DEMO / "uploaded_identity_scan.pdf")
        self.assertEqual(scanned.records[0].method, "pdf_ocr")
        self.assertIn("C-10442", " ".join(r.text for r in scanned.records))
        shot = read_source(DEMO / "customer_admin_console.png")
        self.assertIn("d.okafor@example.com", " ".join(r.text for r in shot.records))
        self.assertTrue(all(0 <= r.confidence <= 1 for r in shot.records))


class MatchingTests(unittest.TestCase):
    def match(self, text: str, **who) -> list:
        subject = scanner.Subject(who.get("emails", []), who.get("phones", []), who.get("names", []), who.get("ids", []))
        record = scanner.Record("s", "line 1", text=text, method=who.get("method", "text"))
        return scanner.match_record(record, subject, subject.hashes())

    def test_exact_identifiers_in_any_format(self) -> None:
        self.assertEqual(self.match("Mail D.Okafor@Example.com", emails=["d.okafor@example.com"])[0].strength, "strong")
        self.assertTrue(self.match("call 07700 900123", phones=["+44 7700 900123"]))
        digest = hashlib.sha256(b"d.okafor@example.com").hexdigest()
        self.assertIn("sha256", self.match(f"hash={digest}", emails=["d.okafor@example.com"])[0].how)
        self.assertTrue(self.match("customer C-10442 closed", ids=["C-10442"]))
        self.assertFalse(self.match("customer C-104421 closed", ids=["C-10442"]))

    def test_names_and_ocr_near_misses_are_only_possible_matches(self) -> None:
        self.assertEqual(self.match("Dear Daniel Okafor", names=["Daniel Okafor"])[0].strength, "weak")
        near = self.match("d.okafor@exarnple.com", emails=["d.okafor@example.com"], method="ocr")
        self.assertEqual(near[0].strength, "weak")
        self.assertEqual(self.match("d.okafor@exampIe.com", emails=["d.okafor@example.com"], method="ocr")[0].strength, "weak")
        self.assertFalse(self.match("d.okafor@exarnple.com", emails=["d.okafor@example.com"]))   # not OCR: no guess


class DemoCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.meta = scanner.read_manifest(ROOT / "examples" / "acme_manifest.csv")
        cls.result = scan(DEMO, meta=cls.meta, emails=["d.okafor@example.com"])

    def finding(self, source: str, location: str = "") -> dict:
        return next(f for f in self.result["findings"] if f["source"].startswith(source) and location in f["location"])

    def test_old_email_and_ids_are_linked_with_proof(self) -> None:
        linked = {i["value"]: i for i in self.result["subject"] if i["origin"] == "linked"}
        self.assertIn("daniel.okafor@example.net", linked)
        self.assertIn("identity change", linked["daniel.okafor@example.net"]["how"])
        self.assertIn("C-10442", linked)

    def test_staff_identifiers_are_never_linked(self) -> None:
        values = {i["value"] for i in self.result["subject"] + self.result["suggested_identifiers"]}
        self.assertNotIn("agent.kim@example.com", values)

    def test_same_name_stranger_is_only_a_possible_match(self) -> None:
        stranger = self.finding("crm_customers.xlsx", "row 3")
        self.assertEqual(stranger["verdict"], "possible match")

    def test_soft_delete_tombstone_hash_and_analytics_id_still_count(self) -> None:
        self.assertEqual(self.finding("crm_customers.xlsx", "Customers, row 2")["verdict"], "violation")
        self.assertIn("soft delete", self.finding("crm_customers.xlsx", "Customers, row 2")["state_reason"])
        tombstone = self.finding("app_production.db", "deleted_users")
        self.assertEqual((tombstone["category"], tombstone["verdict"]), ("ACCOUNT_TOMBSTONE", "violation"))
        self.assertEqual(self.finding("app_production.db", "analytics_events")["verdict"], "violation")

    def test_retained_records_are_compliant(self) -> None:
        self.assertEqual(self.finding("invoice_2025_118.pdf")["verdict"], "compliant")
        self.assertEqual(self.finding("security_audit.csv")["verdict"], "compliant")

    def test_recreation_paths_from_sync_and_backup(self) -> None:
        paths = {p["kind"]: p for p in self.result["recreation_paths"]}
        newsletter = self.finding("newsletter_subscribers.csv")["id"]
        self.assertIn(newsletter, paths["sync"]["can_recreate"])
        self.assertEqual(paths["sync"]["destination_category"], "MARKETING_PROFILE")
        screenshot = self.finding("customer_admin_console.png")["id"]
        self.assertNotIn(screenshot, paths["restore"]["can_recreate"])   # a backup does not restore a screenshot

    @unittest.skipUnless(HAVE_OCR, "needs tesseract")
    def test_document_text_is_one_finding_per_page(self) -> None:
        shots = by_source(self.result, "customer_admin_console.png")
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["location"], "whole image")

    def test_plan_stops_processes_before_changing_records(self) -> None:
        remediation = [s["operation"] for s in self.result["plan"] if s["phase"] == "REMEDIATION"]
        self.assertEqual(remediation[:2], ["DISABLE_DATA_FLOW", "BLOCK_RESTORE"])
        self.assertEqual([s["step_no"] for s in self.result["plan"]], list(range(1, len(self.result["plan"]) + 1)))

    def test_whatif_on_a_check(self) -> None:
        r = self.result
        plan_actions = [{"action": s["operation"], "target": s["resource_id"]} for s in r["plan"] if s["phase"] == "REMEDIATION"]
        self.assertEqual(scanner.whatif_assess(r, plan_actions)["would_be"], "DELETED")
        partial = scanner.whatif_assess(r, [{"action": "DELETE_RECORD", "target": self.finding("newsletter_subscribers.csv")["id"]}])
        self.assertEqual(partial["would_be"], "STILL_EXISTS")
        self.assertTrue(partial["order_warnings"])
        invoice = self.finding("invoice_2025_118.pdf")["id"]
        conflict = scanner.whatif_assess(r, [{"action": "DELETE_RECORD", "target": invoice}])
        self.assertEqual(conflict["outcome"]["policy conflict"][0]["id"], invoice)
        ask = scanner.interpret_question(r, "What if I delete the support ticket?")
        self.assertTrue(ask["clarify"])                                  # two support items, singular noun
        plural = scanner.interpret_question(r, "delete the support messages")
        self.assertGreaterEqual(len(plural["accepted"]), 2)
        self.assertEqual(scanner.validate_action(r, "DELETE_RECORD", "F999")["status"], "rejected")
        everything = scanner.interpret_question(r, "apply the plan")
        self.assertEqual(len(everything["accepted"]), len(plan_actions))


class TimeAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ghosttrace-time-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def test_as_of_ignores_later_sources(self) -> None:
        (self.dir / "jan.csv").write_text("email\nx@example.com\n", encoding="utf-8")
        (self.dir / "mar.csv").write_text("email\ny@example.com\n", encoding="utf-8")
        meta = {"jan.csv": {"captured_at": "2026-01-01T00:00:00Z"}, "mar.csv": {"captured_at": "2026-03-01T00:00:00Z"}}
        result = scan(self.dir, "2026-02-01T00:00:00Z", meta, emails=["x@example.com"])
        self.assertEqual(result["coverage"]["after_as_of"], ["mar.csv"])
        self.assertEqual(result["status"], "STILL_EXISTS")

    def test_latest_observation_of_a_copy_decides(self) -> None:
        (self.dir / "lookups.json").write_text(json.dumps({"rows": [
            {"resource_id": "account_7", "email": "x@example.com", "result": "FOUND", "observed_at": "2026-01-01T00:00:00Z"},
            {"resource_id": "account_7", "email": "x@example.com", "result": "NOT_FOUND", "observed_at": "2026-02-01T00:00:00Z"}]}),
            encoding="utf-8")
        result = scan(self.dir, emails=["x@example.com"])
        verdicts = sorted(f["verdict"] for f in result["findings"])
        self.assertEqual(verdicts, ["compliant", "history"])
        self.assertEqual(result["status"], "DELETED")

    def test_complete_snapshot_proves_removal(self) -> None:
        (self.dir / "users_jan.csv").write_text("id,email\nU1,x@example.com\nU2,z@example.com\n", encoding="utf-8")
        (self.dir / "users_feb.csv").write_text("id,email\nU2,z@example.com\n", encoding="utf-8")
        meta = {"users_jan.csv": {"captured_at": "2026-01-01T00:00:00Z", "store": "users", "complete": True},
                "users_feb.csv": {"captured_at": "2026-02-01T00:00:00Z", "store": "users", "complete": True}}
        result = scan(self.dir, meta=meta, emails=["x@example.com"])
        self.assertEqual(result["findings"][0]["absent_in"]["source"], "users_feb.csv")
        self.assertEqual(result["status"], "DELETED")
        meta["users_feb.csv"]["complete"] = False   # without the flag a missing row proves nothing
        meta["users_jan.csv"]["complete"] = False
        self.assertEqual(scan(self.dir, meta=meta, emails=["x@example.com"])["status"], "STILL_EXISTS")

    def test_expired_security_record_must_go(self) -> None:
        (self.dir / "security_audit.csv").write_text(
            "event_id,user_id,event,retention_until\ns1,U-1001,login,2026-01-01T00:00:00Z\n", encoding="utf-8")
        inside = scan(self.dir, "2025-06-01T00:00:00Z", {"security_audit.csv": {"captured_at": "2025-05-01T00:00:00Z"}}, ids=["U-1001"])
        after = scan(self.dir, "2026-06-01T00:00:00Z", {"security_audit.csv": {"captured_at": "2025-05-01T00:00:00Z"}}, ids=["U-1001"])
        self.assertEqual(inside["findings"][0]["policy"], "RETAIN")
        self.assertEqual(after["findings"][0]["policy"], "DELETE")

    def test_custom_policy_overrides_a_category(self) -> None:
        (self.dir / "newsletter.csv").write_text("email\nx@example.com\n", encoding="utf-8")
        policy = self.dir.parent / f"{self.dir.name}-policy.csv"
        policy.write_text("rule_id,data_category,condition,required_action\nACME-1,MARKETING_PROFILE,Kept 30 days,RETAIN\n",
                          encoding="utf-8")
        try:
            result = scan(self.dir, policy=policy, emails=["x@example.com"])
        finally:
            policy.unlink()
        self.assertEqual(result["findings"][0]["rule_id"], "ACME-1")
        self.assertEqual(result["status"], "DELETED")

    def test_verdicts_when_nothing_or_only_names_match(self) -> None:
        (self.dir / "people.csv").write_text("name,email\nAna Silva,ana@example.com\n", encoding="utf-8")
        self.assertEqual(scan(self.dir, emails=["nobody@example.com"])["status"], "NO_TRACE")
        self.assertEqual(scan(self.dir, names=["Ana Silva"])["status"], "NEEDS_REVIEW")

    def test_suggestions_need_confirmation(self) -> None:
        (self.dir / "tickets.json").write_text(json.dumps({"tickets": [
            {"ticket_id": "T1", "customer_email": "x@example.com", "customer_phone": "+44 7700 900999"}]}), encoding="utf-8")
        (self.dir / "sms_log.csv").write_text("to,body\n+44 7700 900999,Your order shipped\n", encoding="utf-8")
        first = scan(self.dir, emails=["x@example.com"])
        self.assertEqual([i["value"] for i in first["suggested_identifiers"]], ["+44 7700 900999"])
        self.assertFalse(by_source(first, "sms_log.csv"))
        confirmed = scan(self.dir, emails=["x@example.com"], confirmed=["phone:447700900999"])
        self.assertTrue(by_source(confirmed, "sms_log.csv"))
        rejected = scan(self.dir, emails=["x@example.com"], rejected=["phone:447700900999"])
        self.assertFalse(rejected["suggested_identifiers"])

    def test_batch_handles_intent(self) -> None:
        (self.dir / "people.csv").write_text("email\nx@example.com\n", encoding="utf-8")
        sources = scanner.load_sources([self.dir], None, base=self.dir)
        rows = [{"request_id": "A", "email": "x@example.com", "request_text": "Delete my account"},
                {"request_id": "B", "email": "x@example.com", "request_text": "Do you hold my data?"},
                {"request_id": "C", "email": "x@example.com", "request_text": "Change my address, keep my account active"},
                {"request_id": "D", "email": "none@example.com", "request_text": "Erase me"}]
        statuses = [r["status"] for r in scanner.batch(sources, rows)]
        self.assertEqual(statuses, ["STILL_EXISTS", "NEEDS_REVIEW", "NOT_APPLICABLE", "NO_TRACE"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from ghosttrace.server import App, make_handler
        from ghosttrace.workspace import Workspace
        from http.server import ThreadingHTTPServer
        cls.dir = Path(tempfile.mkdtemp(prefix="ghosttrace-ws-"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = cls.httpd.server_address[1]
        cls.httpd.RequestHandlerClass = make_handler(App(Workspace(cls.dir), ROOT), port)
        cls.port = port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.dir)

    def call(self, method: str, path: str, body: bytes | None = None, host: str | None = None) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data

    def test_page_is_local_and_locked_down(self) -> None:
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"http://", body.replace(b"http://www.w3.org", b""))
        self.assertEqual(self.call("GET", "/api/status", host="attacker.example")[0], 403)

    def test_upload_check_whatif_and_export(self) -> None:
        status, _, data = self.call("POST", "/api/sources?name=../../etc/people.csv",
                                    b"email,phone\nx@example.com,+44 7700 900321\n")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["name"], "people.csv")          # no path traversal
        self.assertTrue((self.dir / "sources" / "people.csv").is_file())
        status, _, data = self.call("POST", "/api/check", json.dumps({"emails": ["x@example.com"]}).encode())
        check = json.loads(data)
        self.assertEqual(check["result"]["status"], "STILL_EXISTS")
        status, _, data = self.call("POST", "/api/whatif", json.dumps({
            "check_id": check["id"], "actions": [{"action": "DELETE_RECORD", "target": "F1"}]}).encode())
        self.assertEqual(json.loads(data)["assessment"]["would_be"], "DELETED")
        status, headers, data = self.call("GET", f"/api/checks/{check['id']}/export.csv")
        self.assertIn("people.csv", data.decode())
        self.assertEqual(self.call("GET", "/api/checks/..%2F..%2Fsecret")[0], 404)


if __name__ == "__main__":
    unittest.main()
