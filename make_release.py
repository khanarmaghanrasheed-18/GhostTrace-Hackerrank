"""Build the zip you give to users: python make_release.py

It runs the tests first and stops if any fail. The zip holds only what a user
needs to run GhostTrace: the app, the launchers, the README and the fictional
demo company. No tests, caches, workspaces or results are included.

    dist/GhostTrace-<version>.zip
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "code"))
from ghosttrace.tools import VERSION  # noqa: E402

INCLUDE = ["README.md", "LICENSE", "Start GhostTrace.bat", "start-ghosttrace.command", "start-ghosttrace.sh",
           "code/main.py", "code/ghosttrace", "examples/acme_demo", "examples/acme_manifest.csv",
           "examples/acme_requests.csv"]
EXECUTABLE = {"start-ghosttrace.command", "start-ghosttrace.sh"}


def files_to_ship() -> list[Path]:
    out = []
    for item in INCLUDE:
        path = ROOT / item
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            for found in sorted(path.rglob("*")):
                if found.is_file() and "__pycache__" not in found.parts and not found.name.startswith("."):
                    out.append(found)
    return out


def main() -> int:
    if "--skip-tests" not in sys.argv:
        print("Running the tests…")
        tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "code" / "tests"),
                                "-p", "test_*.py"], capture_output=True, text=True)
        if tests.returncode != 0:
            print(tests.stderr[-3000:])
            print("Tests failed: no release was built.")
            return 1
        summary = [line for line in tests.stderr.splitlines() if line.startswith("Ran ")]
        print(f"{summary[-1] if summary else 'Tests'}: OK")
    if not (ROOT / "LICENSE").is_file():
        print("Note: there is no LICENSE file yet, so the zip has none.")
    folder = f"GhostTrace-{VERSION}"
    target = ROOT / "dist" / f"{folder}.zip"
    target.parent.mkdir(exist_ok=True)
    files = files_to_ship()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = f"{folder}/{path.relative_to(ROOT).as_posix()}"
            info = zipfile.ZipInfo.from_file(path, name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.name in EXECUTABLE:
                info.external_attr = 0o100755 << 16   # double-clickable on macOS and Linux
            archive.writestr(info, path.read_bytes())
    print(f"Built {target.relative_to(ROOT)}: {len(files)} files, {target.stat().st_size // 1024} KB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
