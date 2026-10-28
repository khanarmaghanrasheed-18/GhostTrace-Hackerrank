"""Long-running work in the background: reading files into the index, batch checks.

The web page starts a job, then asks for its progress every second or so, so the
page stays responsive while millions of rows are read. Jobs live only while the
server runs; their results are saved by the work itself where they matter.
"""

from __future__ import annotations

from datetime import datetime, timezone
import itertools
import threading
import time
import traceback
from typing import Any, Callable

KEEP = 30   # finished jobs remembered for the page


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Job:
    def __init__(self, job_id: str, kind: str, label: str):
        self.id = job_id
        self.kind = kind
        self.label = label
        self.status = "running"            # running, done or failed
        self.started = time.time()
        self.started_at = now()
        self.finished_at = ""
        self.progress: dict[str, Any] = {}
        self.result: Any = None
        self.error = ""
        self.lock = threading.Lock()

    def update(self, **values: Any) -> None:
        with self.lock:
            self.progress.update(values)

    def as_dict(self, with_result: bool = False) -> dict[str, Any]:
        with self.lock:
            out = {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                   "started_at": self.started_at, "finished_at": self.finished_at,
                   "seconds": round(time.time() - self.started, 1), "progress": dict(self.progress),
                   "error": self.error}
            if with_result:
                out["result"] = self.result
            return out


class Jobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, Job] = {}
        self.counter = itertools.count(1)

    def start(self, kind: str, label: str, work: Callable[[Job], Any]) -> Job:
        job = Job(f"job-{next(self.counter)}", kind, label)
        with self.lock:
            self.items[job.id] = job
            finished = [j for j in self.items.values() if j.status != "running"]
            for old in finished[:-KEEP]:
                del self.items[old.id]

        def run() -> None:
            try:
                result = work(job)
                with job.lock:
                    job.result, job.status = result, "done"
            except Exception as exc:   # reported on the page; the server keeps running
                traceback.print_exc()
                with job.lock:
                    job.error, job.status = f"{type(exc).__name__}: {exc}", "failed"
            with job.lock:
                job.finished_at = now()

        threading.Thread(target=run, name=job.id, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.items.get(job_id)

    def running(self, kind: str) -> Job | None:
        with self.lock:
            return next((j for j in self.items.values() if j.kind == kind and j.status == "running"), None)

    def listing(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = list(self.items.values())
        return [j.as_dict() for j in reversed(jobs)]
