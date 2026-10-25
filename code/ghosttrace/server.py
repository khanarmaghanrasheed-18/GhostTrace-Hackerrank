"""Local web server for the GhostTrace dashboard and graph explorer.

It listens on 127.0.0.1 only and refuses requests addressed to any other host
name, so pages on other sites cannot reach it. The page loads no outside
scripts, fonts or images. Files, results and settings stay in the workspace
folder. The only outbound call it can make is to Gemini, and only after the user
turns the AI helper on and a GEMINI_API_KEY is set.
"""

from __future__ import annotations

import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import traceback
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import scanner, whatif
from .classify import CATEGORIES
from .gemini import GeminiInterpreter
from .readers import SUPPORTED
from .workspace import Workspace

UI_DIR = Path(__file__).parent / "ui"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/graph.js": ("graph.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
MAX_BODY = 300 * 1024 * 1024


class App:
    def __init__(self, workspace: Workspace, repo_root: Path):
        self.workspace = workspace
        self.repo_root = repo_root
        self.gemini = GeminiInterpreter(workspace.root)
        self._engine = None
        self._benchmark_rows: list[dict[str, Any]] | None = None

    def ai(self) -> Any:
        """The Gemini helper, only when the user allowed it and a key is configured."""
        return self.gemini if self.workspace.settings().get("ai_allowed") and self.gemini.ready else None

    def status(self) -> dict[str, Any]:
        sources = self.workspace.source_list()
        checks = self.workspace.list_checks()
        by_status: dict[str, int] = {}
        for check in checks:
            by_status[check["status"]] = by_status.get(check["status"], 0) + 1
        return {"workspace": str(self.workspace.root), "sources": len(sources),
                "records": sum(s["records"] for s in sources),
                "unreadable": sum(1 for s in sources if not s["readable"]), "checks": len(checks),
                "checks_by_status": by_status, "recent_checks": checks[:8],
                "ai": {"key_configured": self.gemini.ready, "allowed": bool(self.workspace.settings().get("ai_allowed")),
                       "usage": self.gemini.usage()},
                "supported": sorted(SUPPORTED), "categories": CATEGORIES,
                "benchmark_available": (self.repo_root / "dataset" / "evidence_files.csv").is_file()}

    def check(self, body: dict[str, Any]) -> dict[str, Any]:
        lists = {k: [v.strip() for v in body.get(k, []) if str(v).strip()] for k in ("emails", "phones", "names", "ids")}
        subject = scanner.Subject(lists["emails"], lists["phones"], lists["names"], lists["ids"],
                                  confirmed=body.get("confirmed", []), rejected=body.get("rejected", []))
        if not subject.items:
            raise ValueError("enter at least one email, phone, name or ID")
        sources = self.workspace.sources()
        if not sources:
            raise ValueError("add some files on the Sources page first")
        result = scanner.run_check(sources, scanner.Check(subject, body.get("as_of", ""), self.workspace.policy_file(),
                                                          self.workspace.meta()))
        request = {**lists, "as_of": body.get("as_of", ""), "confirmed": body.get("confirmed", []),
                   "rejected": body.get("rejected", []), "label": body.get("label", "")}
        check_id = self.workspace.save_check(request, result)
        return {"id": check_id, "result": result, "request": request}

    def whatif(self, body: dict[str, Any]) -> dict[str, Any]:
        saved = self.workspace.get_check(str(body.get("check_id", "")))
        if not saved:
            raise ValueError("unknown check")
        result = saved["result"]
        actions, messages = [], []
        for item in body.get("actions", []):
            check = scanner.validate_action(result, item.get("action", ""), item.get("target"))
            (actions if check["status"] == "accepted" else messages).append(check)
        interpretation = None
        if body.get("question"):
            interpretation = scanner.interpret_question(result, body["question"], self.ai())
            for item in interpretation["accepted"]:
                actions = [a for a in actions if a["target"] != item["target"]] + [item]
        clean = [{"action": a["action"], "target": a["target"]} for a in actions]
        assessment = scanner.whatif_assess(result, clean)
        return {"actions": clean, "messages": messages, "interpretation": interpretation,
                "assessment": assessment, "explanation": scanner.explain_whatif(assessment)}

    def engine(self) -> Any:
        if self._engine is None:
            from .engine import AuditEngine
            self._engine = AuditEngine(self.repo_root, use_gemini=False)
        return self._engine

    def benchmark_requests(self) -> dict[str, dict[str, str]]:
        from .ingest import read_csv
        rows = {}
        for name, sample in (("sample_deletion_requests.csv", True), ("deletion_requests.csv", False)):
            for row in read_csv(self.repo_root / "dataset" / name):
                rows[row["case_id"]] = {**row, "sample": sample}
        return rows

    def benchmark_list(self) -> list[dict[str, Any]]:
        if self._benchmark_rows is None:
            engine = self.engine()
            rows = []
            for case_id, request in sorted(self.benchmark_requests().items(), key=lambda kv: int(kv[0].split("_")[1])):
                row, detail = engine.investigate(request)
                rows.append({"case_id": case_id, "sample": request["sample"], "email": request["email"],
                             "reported_status": request["reported_status"], "audit_at": request["audit_at"],
                             "status": row["deletion_status"], "steps": len(json.loads(row["steps_json"])),
                             "person": detail.get("user_id")})
            self._benchmark_rows = rows
        return self._benchmark_rows

    def benchmark_case(self, case_id: str) -> dict[str, Any]:
        from .views import case_graph, case_timeline, graph_for_ui
        request = self.benchmark_requests().get(case_id)
        if not request:
            raise ValueError("unknown benchmark case")
        engine = self.engine()
        row, detail = engine.investigate(request)
        view = case_graph(engine, request)
        return {"request": request, "status": row["deletion_status"], "steps": json.loads(row["steps_json"]),
                "intent": detail.get("intent"), "graph": graph_for_ui(engine, view), "view": view,
                "timeline": case_timeline(engine, request)}

    def benchmark_whatif(self, case_id: str, body: dict[str, Any]) -> dict[str, Any]:
        request = self.benchmark_requests().get(case_id)
        if not request:
            raise ValueError("unknown benchmark case")
        model = whatif.CaseModel(self.engine(), request)
        actions, messages = [], []
        for item in body.get("actions", []):
            check = whatif.validate(model, item.get("action", ""), item.get("target"))
            (actions if check["status"] == "accepted" else messages).append(check)
        interpretation = None
        if body.get("question"):
            interpretation = whatif.interpret(model, body["question"], self.ai())
            actions += interpretation["accepted"]
        actions = whatif.add_actions([], actions)
        result = whatif.assess(model, actions)
        return {"actions": [{"action": a["action"], "target": a["resource_id"]} for a in actions],
                "messages": messages, "interpretation": interpretation, "assessment": result,
                "explanation": whatif.explain(result)}


def findings_csv(saved: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "verdict", "category", "policy", "required_state", "state", "source", "location",
                     "matched", "identifying_values", "observed_at", "rule_id"])
    for f in saved["result"]["findings"]:
        writer.writerow([f["id"], f["verdict"], f["category"], f["policy"], f["required_state"], f["state"], f["source"],
                         f["location"], "; ".join(f"{m['identifier']} ({m['how']})" for m in f["matches"]),
                         "; ".join(f["identifying_values"]), f["observed_at"], f["rule_id"]])
    return out.getvalue()


def batch_csv(rows: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["request", "status", "status_text", "still_exists", "needs_review", "where"])
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def make_handler(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "GhostTrace"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass   # keep identifiers out of the terminal log

        def send(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                                                        "style-src 'self'; script-src 'self'; connect-src 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, data: Any, code: int = 200) -> None:
            self.send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("upload is too large")
            return self.rfile.read(length) if length else b""

        def body_json(self) -> dict[str, Any]:
            raw = self.body()
            return json.loads(raw) if raw else {}

        def guard(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self.send(403, b"Forbidden: GhostTrace only answers on localhost.", "text/plain")
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self.guard():
                return
            self.route("GET")

        def do_POST(self) -> None:  # noqa: N802
            if not self.guard():
                return
            self.route("POST")

        def route(self, method: str) -> None:
            url = urlparse(self.path)
            path = unquote(url.path)
            query = parse_qs(url.query)
            try:
                if method == "GET" and path in STATIC:
                    name, kind = STATIC[path]
                    return self.send(200, (UI_DIR / name).read_bytes(), kind)
                ws = app.workspace
                if method == "GET" and path == "/api/status":
                    return self.json(app.status())
                if method == "GET" and path == "/api/sources":
                    return self.json(ws.source_list())
                if method == "POST" and path == "/api/sources":
                    name = ws.add_source(query.get("name", ["upload"])[0], self.body())
                    return self.json(next(s for s in ws.source_list() if s["name"] == name))
                if method == "POST" and path == "/api/sources/settings":
                    data = self.body_json()
                    return self.json(ws.update_source(data["name"], data.get("changes", {})))
                if method == "POST" and path == "/api/sources/delete":
                    return self.json({"deleted": ws.remove_source(self.body_json()["name"])})
                if method == "POST" and path == "/api/manifest":
                    return self.json({"updated": ws.import_manifest(self.body())})
                if method == "POST" and path == "/api/check":
                    return self.json(app.check(self.body_json()))
                if method == "GET" and path == "/api/checks":
                    return self.json(ws.list_checks())
                if method == "GET" and path.startswith("/api/checks/") and path.endswith("/export.csv"):
                    saved = ws.get_check(path.split("/")[3])
                    if not saved:
                        return self.json({"error": "unknown check"}, 404)
                    return self.send(200, findings_csv(saved).encode(), "text/csv; charset=utf-8",
                                     {"Content-Disposition": f'attachment; filename="{saved["id"]}.csv"'})
                if method == "GET" and path.startswith("/api/checks/"):
                    saved = ws.get_check(path.split("/")[3])
                    return self.json(saved) if saved else self.json({"error": "unknown check"}, 404)
                if method == "POST" and path == "/api/checks/delete":
                    return self.json({"deleted": ws.delete_check(self.body_json()["id"])})
                if method == "POST" and path == "/api/whatif":
                    return self.json(app.whatif(self.body_json()))
                if method == "POST" and path == "/api/batch":
                    text = self.body().decode("utf-8-sig")
                    rows = list(csv.DictReader(io.StringIO(text)))
                    results = scanner.batch(ws.sources(), rows, ws.policy_file(), ws.meta())
                    return self.json({"results": results, "csv": batch_csv(results)})
                if method == "GET" and path == "/api/policy":
                    return self.json({"rules": ws.policy(), "custom": ws.policy_file() is not None})
                if method == "POST" and path == "/api/policy":
                    return self.json({"rules": ws.set_policy(self.body()), "custom": True})
                if method == "POST" and path == "/api/policy/reset":
                    ws.reset_policy()
                    return self.json({"rules": ws.policy(), "custom": False})
                if method == "POST" and path == "/api/settings":
                    return self.json(ws.save_settings(self.body_json()))
                if method == "GET" and path == "/api/benchmark":
                    return self.json(app.benchmark_list())
                if method == "GET" and path.startswith("/api/benchmark/"):
                    return self.json(app.benchmark_case(path.split("/")[3]))
                if method == "POST" and path.startswith("/api/benchmark/") and path.endswith("/whatif"):
                    return self.json(app.benchmark_whatif(path.split("/")[3], self.body_json()))
                return self.json({"error": "not found"}, 404)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                return self.json({"error": str(exc)}, 400)
            except Exception as exc:   # report, but keep serving
                traceback.print_exc()
                return self.json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


def serve(workspace: Workspace, repo_root: Path, port: int = 8765, open_browser: bool = False) -> None:
    app = App(workspace, repo_root)
    count = sum(1 for p in workspace.sources_dir.iterdir() if p.is_file())
    if count:
        print(f"Reading {count} files in the workspace (screenshots and scans take a few seconds; results are cached)...")
        workspace.sources()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"GhostTrace is running locally at {url}")
    print(f"Workspace: {workspace.root}")
    print("Nothing is sent anywhere unless you turn on the AI helper. Press Ctrl+C to stop.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
