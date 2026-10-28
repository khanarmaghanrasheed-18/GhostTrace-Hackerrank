"""Local web server for the GhostTrace app.

It listens on 127.0.0.1 only. It refuses requests addressed to any other host name
(so other web pages cannot reach it through DNS rebinding) and requests sent from
pages on other sites. The page loads no outside scripts, fonts or images. Files,
results and settings stay in the workspace folder. The only outbound call it can
make is to Gemini, and only after the user turns the AI helper on and a
GEMINI_API_KEY is set.

Reading files into the index and batch checks run as background jobs; the page
shows their progress.
"""

from __future__ import annotations

import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import string
import traceback
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import report, scanner, whatif
from .classify import CATEGORIES
from .gemini import GeminiInterpreter
from .jobs import Jobs
from .readers import SUPPORTED
from .workspace import Workspace

UI_DIR = Path(__file__).parent / "ui"
UI_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_BODY = 300 * 1024 * 1024           # requests read into memory (JSON, policy, request lists)
PAGE_POLICY = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
               "connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
REPORT_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
DEMO_FILES = ("examples/acme_demo", "examples/acme_manifest.csv")


class App:
    def __init__(self, workspace: Workspace, repo_root: Path):
        self.workspace = workspace
        self.repo_root = repo_root
        self.gemini = GeminiInterpreter(workspace.root)
        self.jobs = Jobs()
        self.read_again = False
        self._engine = None
        self._benchmark_rows: list[dict[str, Any]] | None = None

    def ai(self) -> Any:
        """The Gemini helper, only when the user allowed it and a key is configured."""
        return self.gemini if self.workspace.settings().get("ai_allowed") and self.gemini.ready else None

    # -- reading files ---------------------------------------------------------

    def start_reading(self) -> dict[str, Any]:
        """Read new or changed files into the index in the background. If a read is already
        running, it goes round once more when it finishes, so nothing added meanwhile is missed."""
        running = self.jobs.running("index")
        if running:
            self.read_again = True
            return running.as_dict()
        ws = self.workspace

        def work(job: Any) -> dict[str, Any]:
            while True:
                self.read_again = False
                done = [0]
                job.update(files_total=ws.file_count(), files_done=0, file="", records=0, reading=False)

                def progress(name: str, count: int, seconds: float | None = None) -> None:
                    job.update(file=name, records=count, reading=seconds is None)

                def finished(source: Any) -> None:
                    done[0] += 1
                    job.update(files_done=done[0])

                sources = ws.sources(progress, finished)
                if not self.read_again and ws.ready():
                    break
            return {"files": len(sources), "records": sum(s.count for s in sources)}

        return self.jobs.start("index", "Reading files into the index", work).as_dict()

    def reading(self) -> dict[str, Any] | None:
        job = self.jobs.running("index")
        return job.as_dict() if job else None

    def require_ready(self) -> list[Any]:
        if self.workspace.busy() or self.jobs.running("index"):
            raise ValueError("files are still being read into the index; try again when the progress bar finishes")
        sources = self.workspace.sources()
        if not sources:
            raise ValueError("add some files on the Sources page first")
        return sources

    # -- pages -----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        ws = self.workspace
        sources = ws.source_list(wait=False)
        checks = ws.list_checks()
        by_status: dict[str, int] = {}
        for check in checks:
            by_status[check["status"]] = by_status.get(check["status"], 0) + 1
        return {"workspace": str(ws.root), "sources": len(sources), "folders": len(ws.folders()),
                "records": sum(s["records"] for s in sources),
                "unreadable": sum(1 for s in sources if not s["readable"]), "checks": len(checks),
                "checks_by_status": by_status, "recent_checks": checks[:12], "batches": len(ws.list_batches()),
                "reading": self.reading(),
                "ready": ws.ready() and not ws.busy() and not self.jobs.running("index")
                and not any(s["status"] == "waiting" for s in sources),
                "ai": {"key_configured": self.gemini.ready, "allowed": bool(ws.settings().get("ai_allowed")),
                       "usage": self.gemini.usage()},
                "supported": sorted(SUPPORTED), "categories": CATEGORIES,
                "demo_available": all((self.repo_root / p).exists() for p in DEMO_FILES),
                "benchmark_available": (self.repo_root / "dataset" / "evidence_files.csv").is_file()}

    def check(self, body: dict[str, Any]) -> dict[str, Any]:
        lists = {k: [str(v).strip() for v in body.get(k, []) if str(v).strip()] for k in ("emails", "phones", "names", "ids")}
        subject = scanner.Subject(lists["emails"], lists["phones"], lists["names"], lists["ids"],
                                  confirmed=body.get("confirmed", []), rejected=body.get("rejected", []))
        if not subject.items:
            raise ValueError("enter at least one email, phone, name or ID")
        sources = self.require_ready()
        result = scanner.run_check(sources, scanner.Check(subject, body.get("as_of", ""), self.workspace.policy_file(),
                                                          self.workspace.meta()))
        request = {**lists, "as_of": body.get("as_of", ""), "confirmed": body.get("confirmed", []),
                   "rejected": body.get("rejected", []), "label": body.get("label", "")}
        check_id = self.workspace.save_check(request, result)
        return {"id": check_id, "result": result, "request": request}

    def start_batch(self, name: str, text: str) -> dict[str, Any]:
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise ValueError("the CSV has no rows under its header")
        self.require_ready()
        ws = self.workspace

        def work(job: Any) -> dict[str, Any]:
            job.update(done=0, total=len(rows))
            results = scanner.batch(ws.sources(), rows, ws.policy_file(), ws.meta(),
                                    progress=lambda done, total: job.update(done=done, total=total), keep_results=True)
            for row in results:
                if "result" in row:   # each person's full result can be opened from the batch
                    request = {**row.pop("subject"), "confirmed": [], "rejected": [], "batch": name}
                    row["check_id"] = ws.save_check(request, row.pop("result"))
            return {"batch_id": ws.save_batch(name, len(rows), results)}

        return self.jobs.start("batch", f"Batch check of {name}", work).as_dict()

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

    def preview(self, name: str, offset: int = 0) -> dict[str, Any]:
        """The first records of one file, as GhostTrace read them."""
        if self.workspace.busy():
            raise ValueError("files are still being read; try again in a moment")
        source = next((s for s in self.workspace.sources() if s.name == name), None)
        if source is None:
            raise ValueError("unknown source")
        records = source.store.sample(source.digest, 50, max(offset, 0))
        return {"name": name, "count": source.count, "offset": offset,
                "records": [{"location": r.location, "container": r.container, "fields": r.fields, "text": r.text,
                             "method": r.method, "confidence": round(r.confidence, 2)} for r in records]}

    def add_demo(self) -> dict[str, Any]:
        """Copy the fictional Acme Outdoor demo into the workspace, with its capture times."""
        folder, manifest = (self.repo_root / p for p in DEMO_FILES)
        existing = {p.name for p in self.workspace.sources_dir.iterdir()}
        for path in scanner.collect_files([folder]):
            if path.name not in existing:
                self.workspace.copy_source(path)
        self.workspace.import_manifest(manifest.read_bytes())
        return self.start_reading()

    # -- the benchmark -----------------------------------------------------------

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


def browse(path: str) -> dict[str, Any]:
    """The folders and files in one folder on this computer, for choosing a folder to read."""
    folder = Path(path).expanduser() if path else Path.home()
    if not folder.is_dir():
        raise ValueError(f"not a folder: {folder}")
    folder = folder.resolve()
    entries = []
    try:
        children = sorted(folder.iterdir(), key=lambda c: c.name.lower())
    except OSError as exc:
        raise ValueError(f"cannot open {folder}: {exc.strerror or exc}") from None
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            is_dir = child.is_dir()
            size = 0 if is_dir else child.stat().st_size
        except OSError:
            continue
        entries.append({"name": child.name, "path": str(child), "dir": is_dir, "size": size,
                        "supported": not is_dir and child.suffix.lower() in SUPPORTED})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    if os.name == "nt":
        roots = [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
    else:
        roots = ["/"]
    return {"path": str(folder), "parent": str(folder.parent) if folder.parent != folder else "",
            "home": str(Path.home()), "roots": roots, "entries": entries[:1000], "more": max(len(entries) - 1000, 0),
            "supported_files": sum(1 for e in entries if e["supported"])}


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
    writer = csv.DictWriter(out, fieldnames=["request", "status", "status_text", "still_exists", "needs_review", "where"],
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def make_handler(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "GhostTrace"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass   # keep identifiers out of the terminal log

        def send(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None,
                 policy: str = PAGE_POLICY) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", policy)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, data: Any, code: int = 200) -> None:
            self.send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request is too large")
            return self.rfile.read(length) if length else b""

        def body_json(self) -> dict[str, Any]:
            raw = self.body()
            return json.loads(raw) if raw else {}

        def guard(self, method: str) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self.send(403, b"Forbidden: GhostTrace only answers on localhost.", "text/plain")
                return False
            origin = self.headers.get("Origin")
            cross_site = self.headers.get("Sec-Fetch-Site") == "cross-site"
            if method == "POST" and ((origin and origin not in allowed_origins) or cross_site):
                self.send(403, b"Forbidden: requests from other sites are refused.", "text/plain")
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if self.guard("GET"):
                self.route("GET")

        def do_POST(self) -> None:  # noqa: N802
            if self.guard("POST"):
                self.route("POST")

        def static(self, path: str) -> bool:
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            target = UI_DIR / name
            if "/" in name or target.suffix not in UI_TYPES or not target.is_file():
                return False
            self.send(200, target.read_bytes(), UI_TYPES[target.suffix])
            return True

        def route(self, method: str) -> None:
            url = urlparse(self.path)
            path = unquote(url.path)
            query = parse_qs(url.query)
            ws = app.workspace
            try:
                if method == "GET" and not path.startswith("/api/") and self.static(path):
                    return None
                # -- status, files and folders --
                if method == "GET" and path == "/api/status":
                    return self.json(app.status())
                if method == "GET" and path == "/api/jobs":
                    return self.json(app.jobs.listing())
                if method == "GET" and path.startswith("/api/jobs/"):
                    job = app.jobs.get(path.split("/")[3])
                    return self.json(job.as_dict(with_result=True)) if job else self.json({"error": "unknown job"}, 404)
                if method == "GET" and path == "/api/sources":
                    return self.json(ws.source_list(wait=False))
                if method == "POST" and path == "/api/sources":
                    # Written to disk as it arrives, so the file size is not limited by memory.
                    length = int(self.headers.get("Content-Length") or 0)
                    name = ws.add_upload(query.get("name", ["upload"])[0], self.rfile, length)
                    if query.get("wait"):   # for scripts: read it now and return its summary
                        return self.json(next(s for s in ws.source_list() if s["name"] == name))
                    return self.json({"name": name, "job": app.start_reading()})
                if method == "GET" and path == "/api/sources/preview":
                    return self.json(app.preview(query.get("name", [""])[0], int(query.get("offset", ["0"])[0] or 0)))
                if method == "POST" and path == "/api/sources/settings":
                    data = self.body_json()
                    settings = ws.update_source(data["name"], data.get("changes", {}))
                    app.start_reading()
                    return self.json(settings)
                if method == "POST" and path == "/api/sources/delete":
                    if ws.busy():
                        raise ValueError("files are still being read; try again in a moment")
                    return self.json({"deleted": ws.remove_source(self.body_json()["name"])})
                if method == "POST" and path == "/api/manifest":
                    count = ws.import_manifest(self.body())
                    app.start_reading()
                    return self.json({"updated": count})
                if method == "POST" and path == "/api/demo":
                    return self.json({"job": app.add_demo()})
                if method == "POST" and path == "/api/reread":
                    ws.invalidate()
                    return self.json({"job": app.start_reading()})
                if method == "GET" and path == "/api/folders":
                    return self.json({"folders": ws.folders()})
                if method == "GET" and path == "/api/browse":
                    return self.json(browse(query.get("path", [""])[0]))
                if method == "POST" and path == "/api/folders":
                    folders = ws.link_folder(str(self.body_json().get("path", "")))
                    return self.json({"folders": folders, "job": app.start_reading()})
                if method == "POST" and path == "/api/folders/remove":
                    if ws.busy():
                        raise ValueError("files are still being read; try again in a moment")
                    return self.json({"folders": ws.unlink_folder(str(self.body_json().get("path", "")))})
                # -- checks --
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
                if method == "GET" and path.startswith("/api/checks/") and path.endswith("/report.html"):
                    saved = ws.get_check(path.split("/")[3])
                    if not saved:
                        return self.json({"error": "unknown check"}, 404)
                    return self.send(200, report.render(saved).encode(), "text/html; charset=utf-8", policy=REPORT_POLICY)
                if method == "GET" and path.startswith("/api/checks/"):
                    saved = ws.get_check(path.split("/")[3])
                    return self.json(saved) if saved else self.json({"error": "unknown check"}, 404)
                if method == "POST" and path == "/api/checks/delete":
                    return self.json({"deleted": ws.delete_check(self.body_json()["id"])})
                if method == "POST" and path == "/api/whatif":
                    return self.json(app.whatif(self.body_json()))
                # -- batches --
                if method == "POST" and path == "/api/batch":
                    name = query.get("name", ["requests.csv"])[0]
                    return self.json({"job": app.start_batch(name, self.body().decode("utf-8-sig"))})
                if method == "GET" and path == "/api/batches":
                    return self.json(ws.list_batches())
                if method == "GET" and path.startswith("/api/batches/") and path.endswith("/export.csv"):
                    saved = ws.get_batch(path.split("/")[3])
                    if not saved:
                        return self.json({"error": "unknown batch"}, 404)
                    return self.send(200, batch_csv(saved["results"]).encode(), "text/csv; charset=utf-8",
                                     {"Content-Disposition": f'attachment; filename="{saved["id"]}.csv"'})
                if method == "GET" and path.startswith("/api/batches/"):
                    saved = ws.get_batch(path.split("/")[3])
                    return self.json(saved) if saved else self.json({"error": "unknown batch"}, 404)
                if method == "POST" and path == "/api/batches/delete":
                    return self.json({"deleted": ws.delete_batch(self.body_json()["id"])})
                # -- policy and settings --
                if method == "GET" and path == "/api/policy":
                    return self.json({"rules": ws.policy(), "custom": ws.policy_file() is not None,
                                      "default": [r for _, r in sorted(scanner.load_policy(None).items())]})
                if method == "POST" and path == "/api/policy":
                    return self.json({"rules": ws.set_policy(self.body()), "custom": True})
                if method == "POST" and path == "/api/policy/rules":
                    rules = ws.set_rules(self.body_json().get("rules", []))
                    return self.json({"rules": rules, "custom": ws.policy_file() is not None})
                if method == "POST" and path == "/api/policy/reset":
                    ws.reset_policy()
                    return self.json({"rules": ws.policy(), "custom": False})
                if method == "POST" and path == "/api/settings":
                    return self.json(ws.save_settings(self.body_json()))
                # -- the benchmark --
                if method == "GET" and path == "/api/benchmark":
                    return self.json(app.benchmark_list())
                if method == "GET" and path.startswith("/api/benchmark/"):
                    return self.json(app.benchmark_case(path.split("/")[3]))
                if method == "POST" and path.startswith("/api/benchmark/") and path.endswith("/whatif"):
                    return self.json(app.benchmark_whatif(path.split("/")[3], self.body_json()))
                return self.json({"error": "not found"}, 404)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                return self.json({"error": str(exc).strip("'\"")}, 400)
            except Exception as exc:   # report, but keep serving
                traceback.print_exc()
                return self.json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


def serve(workspace: Workspace, repo_root: Path, port: int = 8765, open_browser: bool = False) -> None:
    app = App(workspace, repo_root)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"GhostTrace is running locally at {url}")
    print(f"Workspace: {workspace.root}")
    print("Nothing is sent anywhere unless you turn on the AI helper. Press Ctrl+C to stop.")
    if workspace.file_count():
        print("Reading new or changed files into the local index; the page shows the progress.")
    app.start_reading()
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
