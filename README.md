# GhostTrace

**Is that person's data really deleted?** GhostTrace is a local agent that answers that question for any pile of company data:

- spreadsheets and database exports;
- screenshots and scanned PDFs;
- logs, support exports, Word letters and email files.

Give it the files and the person's email, phone, name or ID. It tells you:

- **whether their data still exists**, and the exact file, sheet, table, row, page or log line where it remains;
- **why** it counts: which identifier matched, how GhostTrace knows that identifier belongs to the person, and which policy rule applies;
- **what can bring it back**: active syncs and integrations, and restorable backups;
- **how to finish the job**: an ordered cleanup plan that stops processes first, then deletes, redacts or anonymizes, then verifies, while keeping what must be kept (invoices, security logs, legal holds);
- **what a cleanup idea would achieve** before anyone touches a system (what-if analysis).

It runs entirely on your computer. Nothing is uploaded and no outside scripts are loaded. Checking files never calls an AI service, so there are no credits or per-file costs; the optional AI helper (for what-if questions only) is off until you turn it on.

It is built for large data: each file is read once, row by row, into a local index, so millions of rows fit on an ordinary computer.

---

## Quick start

You need Python 3.10 or newer; GhostTrace uses only Python's standard library. To read PDFs and screenshots, also install two free command-line tools:

```bash
# Debian / Ubuntu
sudo apt-get install -y poppler-utils tesseract-ocr
# macOS (Homebrew)
brew install poppler tesseract
# Windows: install Tesseract (UB Mannheim build) and Poppler, and add both to PATH
```

Start the app:

| System | How |
| --- | --- |
| Windows | Double-click **`Start GhostTrace.bat`** |
| macOS | Double-click **`start-ghosttrace.command`** (the first time: right-click it, then *Open*) |
| Linux | Run `./start-ghosttrace.sh` |
| Any | `python3 code/main.py serve --open` (on Windows `py` instead of `python3`) |

Your browser opens **http://127.0.0.1:8765/**. Leave the terminal window open while you use the app; closing it stops GhostTrace.

To try it without your own data:

1. On **Home**, click **Try the demo company**.
2. Check `d.okafor@example.com`.

The demo is a fictional company with 12 files of every kind about one customer.

Your files and results live in `./ghosttrace_workspace/`. Use `--workspace PATH` to keep them somewhere else, and `--port` to change the port.

### Large data

Big database files and exports do not need to be copied into the workspace. Link their folders instead. GhostTrace reads them where they are and never changes or deletes them:

```bash
python3 code/main.py serve --link /data/exports/crm /data/exports/app_db
# Windows: py code/main.py serve --link D:\exports\crm D:\exports\app_db
```

You can also add a folder on the **Sources** page.

How it works:

- Each file is read once, row by row, into a local index (`<workspace>/.cache/index.sqlite`).
- A file is read again only when it changes. Progress is shown in the terminal.
- A check looks the person up in the index instead of reading every row again.
- Nothing is cut off: every row of every table is read.

Measured with a fictional company database of 5 million rows (four tables, 463 MB) on a 4-core cloud machine:

| | |
| --- | --- |
| First read into the index | about 7 minutes (about 12,000 rows a second); later starts are instant |
| Index size on disk | 1.9 GB |
| A check by email, phone or ID | under 6 seconds, including following the person's links |
| A check by name only | about 4 seconds |
| Memory used | under 60 MB |

A check takes the same few seconds no matter how many people you check in a batch, one after another.

## Using the app

| Page | What you do there |
| --- | --- |
| **Home** | A guided start (add a folder, upload files, or try the demo), then an overview: a quick check box, recent checks, the kinds of data most often left behind, and files that could not be read. |
| **Check a person** | Type emails, phones, IDs and names; each becomes a chip with its kind guessed (you can change it). Optionally check "as of" a date and add a ticket reference. |
| **Result** | A plain-language verdict and what to do next, then tabs: a summary grouped by file, all findings (click one for its evidence: why it matched, the policy decision, the record with the matched values highlighted), the graph, the timeline, the cleanup plan as a checklist, what-if, identifiers (confirm or reject suggested links, then run again), and coverage. Export a printable report, the findings as CSV, or the full result as JSON. |
| **Results** | Every saved check, filterable by verdict and searchable. |
| **Batch check** | Drop a CSV of requests (a template is offered). A progress bar runs while each row is checked; every row then opens as its own full result. Earlier batches are kept. |
| **Sources** | Add a folder to read in place (with a folder browser), upload files, or drop files anywhere on the page. Files are read in the background with live progress. Click a file to preview exactly what was read and to set its capture time, kind of data, store and complete-snapshot flag. |
| **Deletion policy** | Edit the required action for each kind of data, import or export the rules as CSV, or go back to the default. |
| **Settings** | Privacy details and the workspace folder, light or dark theme, the opt-in AI helper, and re-reading all files. |
| **Benchmark** | Explore the bundled Northstar Shop dataset (550 synthetic deletion requests) with the same graph, timeline and what-if tools. |

Press **Ctrl K** (⌘K on a Mac) anywhere to jump to a page, open a recent check, or check someone straight away.

**The graph explorer** shows the person, every identifier, each record or copy, sync and restore processes, and the files they came from. You can:

- drag nodes, pan, and zoom;
- search for any email, ID or file name;
- filter by node type;
- click a node to see its details and every connection with its source and time;
- double-click to reveal neighbours, or use *Focus* to see only what is near a node.

Edges are styled by how they are known:

- *observed* in the data;
- *entered by you*;
- *concluded* by GhostTrace (for example, the old email found in an identity-change row);
- *reported* by a system (a log saying "deleted" is a claim, not proof);
- *suggested* or *possible* (needs a person).

**What-if** lets you propose actions (for example "delete F5", "disable F3"), or ask in plain English: "what if I delete the search entries and switch off the connector?". Proposals add up, and *Reset* clears them. Each run says whether the person's data would then be fully deleted, what is still required, which retained records the plan would wrongly delete, and which sync or backup could undo the change. It is always hypothetical: nothing is changed and the saved result stays as it was.

## Command line

```bash
# one person, any files or folders
python3 code/main.py scan path/to/exports another/file.xlsx --email jane@example.com --id C-1001
python3 code/main.py scan examples/acme_demo --manifest examples/acme_manifest.csv \
        --email d.okafor@example.com --as-of 2026-09-21 --json --out result.json

# many people from a CSV
python3 code/main.py batch-check examples/acme_requests.csv examples/acme_demo --out results.csv
```

Options:

- `--confirm KIND:VALUE` / `--reject KIND:VALUE` answer the suggested links the output lists.
- `--policy rules.csv` uses your own rules.
- `--manifest files.csv` gives capture times.

The command line keeps its index in `./.cache/ghosttrace/index.sqlite`, so a second check on the same files is quick.

## Supported inputs

| Kind | Files |
| --- | --- |
| Spreadsheets | `.csv`, `.tsv` (delimiter detected), `.xlsx`, `.xlsm` (every sheet) |
| Databases | SQLite files (`.db`, `.sqlite`, `.sqlite3`, or any file with an SQLite header) and SQL dumps (`.sql`). Dumps in other SQL dialects are scanned line by line. |
| Structured data | `.json` (nested lists at any depth), `.jsonl` / `.ndjson`, `.xml` |
| Documents | `.pdf` (text layer, or OCR for scans), `.docx`, `.pptx`, `.eml` (headers and body), `.mbox` mailbox exports (every message), `.txt`, `.md`, `.html`, `.log` (eight common log layouts, including JSON lines and syslog) |
| Contacts and calendars | `.vcf` (one record per contact) and `.ics` (one record per event, with organizer and attendees) |
| Images | `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.gif`, `.webp`, read by OCR with a confidence for each line |
| Archives | `.zip`, whose members are read with the same rules |

Spreadsheets, databases, SQL dumps, JSON Lines and text or log files of any size are read row by row. Formats that have to be parsed whole (JSON, XML, HTML, Office files, PDFs, email and images) are limited to 300 MB each; save a large JSON export as JSON Lines.

Files that cannot be read (old binary `.xls`, damaged files, a missing OCR tool) are listed under **Coverage**, and a clean verdict is never given while they remain unread.

## How GhostTrace decides

1. **Finding the person.**
   - **Proof:** an exact email (in any letter case), a phone number in any format (matched on the last 9 digits), an ID, or a SHA-256, SHA-1 or MD5 hash of their email or phone.
   - **Never proof:** a name only gives a *possible match*. A near-miss email in OCR text ("exampIe.com") is also only *possible*. So is a phone number found on the profiles of different people (a household, switchboard or placeholder number): GhostTrace never follows it to other people's records.
   - A check lists at most 50 possible matches one by one and counts the rest. A common name in a large customer base can match thousands of strangers.
2. **Following links it can prove.**
   - **Used automatically:** the other identifiers on a matched *profile* row, and both values of an *identity-change* row (old and new email).
   - **Suggested for you to confirm:** values that merely sit next to the person's data, such as a phone on a support ticket.
   - **Never linked:** staff columns such as `agent_email` or `author_email`.
   - **Screenshots of forms:** `Label: value` lines (and common labels such as `Customer ID   C-10442`) are read as fields, so a details screen links the IDs it shows. Values read by OCR below 80% confidence are only suggested.
3. **Kind of data.** Every match is classified from the following, in order:
   - a `record_type` field;
   - your choice for the file;
   - table, sheet and file names;
   - the column names.

   Kinds: profile, search index, analytics, support message or attachment, export, marketing, email log, security log, legal hold, financial record, backup, sync/integration, cache, tombstone, identifier map, or other personal record.
4. **State at the chosen time.**
   - Later files are ignored when you check "as of" a date.
   - The latest observation of the same stored copy wins.
   - A row marked `deleted` that still holds the data is a *soft delete*, so the data still exists.
   - A lookup that reports "not found" is evidence of absence.
   - A screenshot of a search that found nothing ("No customer found") is only the form's *claim*: a form shows what its query selects and can hide rows marked deleted. On its own it gives *Needs review*. The database files or exports behind the form decide.
   - A missing row proves removal only from a file you marked as a complete snapshot.
5. **Policy.** The default policy (`code/ghosttrace/policy/default_policy.csv`) says:
   - **delete:** profiles, search entries, exports, marketing data, attachments;
   - **redact:** support conversations, email logs, and deleted-account tombstones (the internal ID may stay);
   - **anonymize:** analytics (a customer ID still counts as identifying);
   - **retain:** security logs within their retention period, legal holds, invoices;
   - **block restore on backups, and disable syncs.**

   Your own CSV (`rule_id,data_category,condition,required_action`) replaces any rule it names.
6. **Verdict.**
   - *Still exists:* a copy breaks the policy, or a sync or backup can bring the data back.
   - *Needs review:* only possible matches, unknown states, or unreadable files.
   - *Deleted:* every copy found has the required treatment.
   - *No trace found:* nothing in the readable sources.

   Every result says it is limited to the files you supplied.

## Privacy and security

- The server binds to `127.0.0.1` only, and refuses requests addressed to any other host name (a guard against DNS-rebinding).
- A strict Content-Security-Policy is sent: no outside scripts, styles, fonts or connections.
- All page text is inserted as text, never as HTML, so file contents cannot run code.
- Uploaded file names are reduced to plain names, so there is no path traversal.
- Identifiers are not written to the terminal log.
- **The AI helper (optional):**
  - It needs both a `GEMINI_API_KEY` environment variable and the switch on the Settings page; it is off by default.
  - When on, Gemini only turns what-if questions into proposed actions, and code checks every proposal.
  - Without it, a local rule reader does the same job.
  - `GEMINI_MAX_CALLS` limits calls (default 20).

## Project layout

```text
code/main.py                  command line: serve, scan, batch-check, and the benchmark commands
code/ghosttrace/readers.py    readers for every supported file type
code/ghosttrace/store.py      the on-disk index: each file read once, people looked up by identifier
code/ghosttrace/classify.py   kind of data, state, identifier columns, policy loading
code/ghosttrace/scanner.py    matching, identity links, findings, verdict, plan, graph, timeline, what-if, batch
code/ghosttrace/workspace.py  the local workspace (files, linked folders, settings, saved checks and batches, custom policy)
code/ghosttrace/jobs.py       background jobs (reading files, batch checks) with progress
code/ghosttrace/report.py     the printable HTML report of a check
code/ghosttrace/server.py     local web server and JSON API
code/ghosttrace/ui/           the app: plain HTML, CSS and JavaScript modules, no build step and nothing loaded from outside
Start GhostTrace.bat, start-ghosttrace.command, start-ghosttrace.sh
                              double-click launchers
code/ghosttrace/ingest.py, identity.py, engine.py, graph.py, views.py, whatif.py
                              the benchmark audit engine for the Northstar dataset
code/evaluation/              tests (76) and the benchmark evaluator
examples/                     fictional Acme Outdoor demo, its manifest and a batch request file
docs/PLAN.md                  architecture decisions and status
```

Run the tests with:

```bash
python3 -m unittest discover -s code/evaluation -p 'test_*.py' -v
```

## Benchmark: the Northstar Shop dataset

`dataset/` holds a synthetic deletion-audit benchmark: 550 requests, with 94 evidence files of every kind and hidden answers for 518 of them. The benchmark engine reconstructs each case at its audit time and writes the challenge-format `output.csv`.

```bash
python3 code/main.py run          # all 518 requests -> output.csv
python3 code/main.py validate
python3 code/main.py graph case_29   /   timeline case_29   /   whatif case_29 --add "DISABLE_DATA_FLOW data_flow_3827"
python3 code/evaluation/main.py   # score on the 32 public samples
```

On the public samples the engine gets:

- 32/32 statuses right;
- 21/32 rows exactly right;
- every predicted step correct (step precision 1.0);
- 94% of expected steps found (step recall 0.94).

The general `scan` command reaches the same findings from just an email address, for example:

```bash
python3 code/main.py scan dataset/evidence --manifest dataset/evidence_files.csv \
        --email noah.f438085@outlook.com --as-of 2026-09-15T12:00:00Z
```

The original challenge statement is kept in [`problem_statement.md`](./problem_statement.md); the planning conversation behind the architecture is in [`docs/GHOSTTRACE_PLANNING_CONTEXT.md`](./docs/GHOSTTRACE_PLANNING_CONTEXT.md).

## Limitations

- GhostTrace sees only the files you give it. "No trace found" means nothing was found in those files, not that no copy exists anywhere.
- It reads files, not live database servers. Export MySQL, PostgreSQL or SQL Server tables (an SQL dump, or CSV/Excel per table) and link the folder.
- The index needs disk space, about four times the size of a table export, and the first read of a large file takes a while.
- OCR can misread characters. Near-misses are shown as possible matches, and each OCR line keeps its confidence.
- The kind of data is guessed from names. Correct it per file on the Sources page when the guess is wrong.
- A sync's destination is read from its configured destination or name. If neither says, the destination is reported as unknown. Backup contents are not itemized, so any archived database or file record is treated as recoverable.
- What-if results show what the supplied evidence implies; they are not a guarantee about live systems.
