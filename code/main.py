"""GhostTrace from the terminal.

  serve         start the app in your browser at http://127.0.0.1:8765 (what the launchers run)
  scan          check one person against files or folders and print the verdict
  batch-check   check every person in a CSV against files or folders
  doctor        show whether this computer has everything GhostTrace needs
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

if sys.version_info < (3, 10):
    sys.exit("GhostTrace needs Python 3.10 or newer; this is Python "
             f"{sys.version_info[0]}.{sys.version_info[1]}. Download a newer one from https://www.python.org/downloads/")

from ghosttrace import scanner, tools  # noqa: E402
from ghosttrace.workspace import show_progress  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(prog="ghosttrace", description="Check whether a person's data is really deleted.")
    parser.add_argument("--version", action="version", version=f"GhostTrace {tools.VERSION}")
    sub = parser.add_subparsers(dest="command")
    serve_cmd = sub.add_parser("serve", help="start the app in your browser")
    serve_cmd.add_argument("--workspace", default=str(ROOT / "ghosttrace_workspace"),
                           help="folder for uploaded files, the index, results and settings")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.add_argument("--add", nargs="*", default=[], help="files or folders to copy into the workspace first")
    serve_cmd.add_argument("--link", nargs="*", default=[],
                           help="folders to read in place without copying them (best for large data)")
    serve_cmd.add_argument("--manifest", help="CSV with file_path and captured_at for the added files")
    serve_cmd.add_argument("--open", action="store_true", help="open the browser")
    scan = sub.add_parser("scan", help="check whether one person's data still exists in files or folders")
    scan.add_argument("sources", nargs="+", help="files or folders")
    for flag in ("email", "phone", "name", "id"):
        scan.add_argument(f"--{flag}", action="append", default=[], help=f"{flag} of the person (repeatable)")
    scan.add_argument("--as-of", default="", help="judge the state at this time (ISO date or time)")
    scan.add_argument("--policy", help="custom policy CSV (rule_id,data_category,condition,required_action)")
    scan.add_argument("--manifest", help="CSV with file_path and captured_at columns")
    scan.add_argument("--confirm", action="append", default=[], help="accept a suggested identifier, e.g. email:x@y.z")
    scan.add_argument("--reject", action="append", default=[], help="reject a suggested identifier")
    scan.add_argument("--json", action="store_true", help="print the full result as JSON")
    scan.add_argument("--out", help="also write the full JSON result to this file")
    batch_cmd = sub.add_parser("batch-check", help="check every person listed in a CSV")
    batch_cmd.add_argument("requests", help="CSV with email/phone/name/user_id columns, optional as_of and request_id")
    batch_cmd.add_argument("sources", nargs="+", help="files or folders")
    batch_cmd.add_argument("--policy")
    batch_cmd.add_argument("--manifest")
    batch_cmd.add_argument("--out", help="write results to this CSV")
    sub.add_parser("doctor", help="show whether this computer has everything GhostTrace needs")
    args = parser.parse_args(sys.argv[1:] or ["serve", "--open"])
    if args.command == "serve":
        return run_serve(args)
    if args.command == "scan":
        return run_scan(args)
    if args.command == "batch-check":
        return run_batch(args)
    if args.command == "doctor":
        return tools.doctor()
    parser.print_help()
    return 2


def cache_dir() -> Path:
    """Where the command line keeps its index of the files it has read."""
    return Path.cwd() / ".cache" / "ghosttrace"


def run_serve(args: argparse.Namespace) -> int:
    from ghosttrace.server import serve
    from ghosttrace.workspace import Workspace
    workspace = Workspace(Path(args.workspace))
    existing = {p.name for p in workspace.sources_dir.iterdir()}
    for item in args.add:
        for path in scanner.collect_files([Path(item)]):
            if path.name not in existing:   # adding the same folder again does not duplicate files
                workspace.copy_source(path)
    for item in args.link:
        workspace.link_folder(item)
    if args.manifest:
        print(f"Capture times read for {workspace.import_manifest(Path(args.manifest).read_bytes())} files.")
    serve(workspace, ROOT, args.port, args.open)
    return 0


def run_scan(args: argparse.Namespace) -> int:
    meta = scanner.read_manifest(Path(args.manifest)) if args.manifest else {}
    paths = [Path(p) for p in args.sources]
    base = paths[0] if len(paths) == 1 and paths[0].is_dir() else None
    sources = scanner.load_sources(paths, cache_dir(), meta, base=base, progress=show_progress)
    subject = scanner.Subject(args.email, args.phone, args.name, args.id, confirmed=args.confirm, rejected=args.reject)
    if not subject.items:
        print("Give at least one --email, --phone, --name or --id.")
        return 2
    result = scanner.run_check(sources, scanner.Check(subject, args.as_of, Path(args.policy) if args.policy else None, meta))
    if args.out:
        Path(args.out).write_text(scanner.to_json(result), encoding="utf-8")
    if args.json:
        print(scanner.to_json(result))
        return 0
    print(render_scan(result))
    return 0


def render_scan(result: dict) -> str:
    c = result["counts"]
    out = [f"VERDICT: {result['status_text']}", result["label"],
           f"{c['records_scanned']} records in {c['sources_used']} of {c['sources']} sources; "
           f"{c['still_exists']} still exist, {c['compliant']} compliant, {c['needs_review']} need review"
           + (f", {c['claims']} form claims" if c.get("claims") else "") + "."]
    out.append("\nIDENTIFIERS")
    for item in result["subject"]:
        out.append(f"  {item['origin']:<9} {item['kind']:<6} {item['value']}  ({item['how']}{'; ' + item['evidence'] if item['evidence'] else ''})")
    for item in result["suggested_identifiers"]:
        out.append(f"  suggested {item['kind']:<6} {item['value']}  ({item['how']}; {item['evidence']}) -> --confirm {item['key']}")
    out.append("\nFINDINGS")
    for f in result["findings"]:
        if f["verdict"] == "history":
            continue
        out.append(f"  {f['id']:<4} {f['verdict']:<14} {f['category']:<19} {f['source']} / {f['location']}")
        if f["verdict"] == "claim":
            out.append(f"       {f['state_reason']}")
            continue
        out.append(f"       {f['state']}: {f['state_reason']}; policy {f['rule_id']} {f['policy']} -> {f['required_state']}")
    for path in result["recreation_paths"]:
        out.append(f"  RECREATION {path['process']} {path['kind']} {path['state']} -> can bring back "
                   f"{', '.join(path['can_recreate']) or 'nothing identified'} ({path['basis']})")
    if c.get("possible_not_listed"):
        out.append(f"  ... and {c['possible_not_listed']:,} more possible matches (name only or a shared phone), not listed")
    if result["coverage"]["unreadable"]:
        out.append(f"\nNOT READABLE (not checked): {', '.join(result['coverage']['unreadable'])}")
    if result["plan"]:
        out.append("\nCLEANUP PLAN")
        for step in result["plan"]:
            out.append(f"  {step['step_no']:>2}. {step['phase']:<12} {step['operation']:<26} {step['resource_id']:<5} {step['where']}")
    return "\n".join(out)


def run_batch(args: argparse.Namespace) -> int:
    from ghosttrace.server import batch_csv
    meta = scanner.read_manifest(Path(args.manifest)) if args.manifest else {}
    paths = [Path(p) for p in args.sources]
    base = paths[0] if len(paths) == 1 and paths[0].is_dir() else None
    sources = scanner.load_sources(paths, cache_dir(), meta, base=base, progress=show_progress)
    with Path(args.requests).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    results = scanner.batch(sources, rows, Path(args.policy) if args.policy else None, meta)
    text = batch_csv(results)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
