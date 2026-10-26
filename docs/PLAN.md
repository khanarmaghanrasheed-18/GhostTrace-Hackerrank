# GhostTrace plan

The architecture comes from the "Agreed Architecture" section of [`GHOSTTRACE_PLANNING_CONTEXT.md`](./GHOSTTRACE_PLANNING_CONTEXT.md). After the hackathon deadline passed, the owner widened the scope. GhostTrace is now a **local deletion-verification agent for any company's data**, and the hackathon dataset is kept as a benchmark. This file records how each decision is implemented and what changed.

## Product scope

- **Input:** any mix of files (spreadsheets, SQLite/SQL, JSON/XML, PDFs, Word, PowerPoint, email, logs, screenshots of forms, zip archives) and one or more people, each given by email, phone, name or ID, singly or as a batch CSV. GhostTrace never connects to a live database; it reads database files, dumps and exports.
- **Scale:** a large company's data. Files are read once, row by row, into an on-disk index; folders can be read in place without copying.
- **Output:** a verdict with every remaining copy and its proof:
  - **Still exists**
  - **Needs review**
  - **Deleted**
  - **No trace found**

  The output also lists recreation paths, an ordered cleanup plan under a default or custom policy, a graph, a timeline, and what-if analysis.
- **Where it runs:** on the user's own computer, as a local web app (`serve`) or the command line (`scan`, `batch-check`). Companies keep their data private, so no hosted deployment.

## Agreed decisions and how they are implemented

| Decision (planning summary) | Implementation |
| --- | --- |
| Modular Python engine, standard library only | `readers`, `classify`, `scanner`, `workspace`, `server` for any data. `ingest`, `identity`, `engine`, `graph`, `views`, `whatif` for the benchmark. No Python packages are needed; PDFs and images need `poppler-utils` and `tesseract`. |
| Direct parsing first, OCR only where needed, cache extraction | Every format is parsed directly. PDFs use the text layer, then OCR for scans. Images use OCR, with a confidence for each line. Each file is read once into the on-disk index (`store.py`), keyed by its content digest, and read again only when it changes. |
| Large data (owner's requirement after the scope change) | Readers stream rows through `Source.add`, so tables, SQL dumps, JSON Lines and logs of any size never sit in memory; there is no record limit (the old 200,000-record cut-off could give a wrong "no trace"). The index keeps lookup keys per record (emails, phones by last nine digits, digests, ID-like tokens); names and other IDs use a text search over the stored records; candidates are confirmed by the same exact matching as before. Each identifier is looked up once per check. A phone found on the profiles of different people is a possible match and is never followed, and at most 50 possible matches are listed. |
| Screenshots of application forms | `Label: value` lines become fields, so a details screen links the IDs it shows (OCR below 80% confidence is only suggested). A search screen that found nothing is a `claim`: alone it gives Needs review; the database files behind the form decide. |
| Preserve source, location, time, method and failures | Each record keeps its file, sheet/table/row/page/line, method, OCR confidence and its own timestamp. Each source keeps its capture time (stated or file-modified) and its errors. Unreadable files block a clean verdict. |
| Grounded identity links; names and similarity never prove identity | Proof: exact email, normalized phone, ID, and email/phone hashes. Names and OCR near-misses are only possible matches. Auto-links come only from profile rows and identity-change rows. Other links are suggestions the user confirms or rejects. Staff columns are never linked. |
| Reconstruct at a cutoff; later evidence excluded | "As of" removes later sources and records. The latest observation of a stored copy wins. Absence counts only from explicit "not found" results or from complete snapshots the user marks. |
| Conflicting observations preserved | Every observation stays in the findings (earlier ones as `history`). Copies in different stores are judged separately. |
| Supplied policy governs deletion, redaction, anonymization, retention | `policy/default_policy.csv` uses the challenge's rule format. A custom CSV replaces the categories it names. Retention periods and tombstones are honoured. Deleting a retained record is flagged as a policy conflict. |
| Evidence graph: people, identifiers, records/copies, processes, evidence; provenance and time on every edge; observed vs claimed vs derived | Built from the same results as the verdict: `scanner.build_graph` for any data, `graph.EvidenceGraph` for the benchmark (the benchmark engine reads its links from that graph). Edge kinds: observed, provided, derived, claimed, suggested, weak. |
| Graph used for identity traversal and recreation paths | Identity expansion repeats until no new proven identifier appears. A recreation path needs an observed open sync or restorable backup tied to the person; its targets are the destination kind or the archived records. |
| Timeline separating event time and capture time, no inferred causes | Dated rows, identity changes and source captures, sorted by time, with capture time in its own column. |
| What-if: structured and natural-language proposals, validation, clarification, temporary copy, cumulative with reset, original untouched, explanations from checked results, optional second LLM call | `scanner.whatif_assess` / `interpret_question` (any data) and `whatif.py` (benchmark). Gemini is used only when allowed; otherwise a labelled local rule reader. Rejected targets get reasons. Singular ambiguous targets ask which item is meant. "Apply the plan" proposes every remediation. The rewording call is optional. |
| Gemini selective and validated; never evidence | Optional and off by default. It needs a key and the Settings switch, because it is the only outbound call. Calls are cached, the call budget is enforced, and usage is shown in Settings. |
| Terminal-first; UI, deployment, MCP and graph database deferred | **Changed by the owner:** a local web UI (dashboard and graph explorer) is now built. It binds to localhost only and loads no outside resources. Deployment stays out of scope on purpose (data must stay on the user's machine). MCP and a separate graph database remain deferred. |
| Exact challenge output schema | Kept for the benchmark (`run` writes `output.csv`, 518 rows, valid). The general agent uses the same phases and operations in its plans. |

## Status

| Part | State |
| --- | --- |
| Benchmark engine (corrected) | Done. Public samples: status 32/32, exact rows 21/32, step precision 1.000, recall 0.938. 518-row output valid. |
| Graph, timeline, what-if (benchmark) | Done |
| General agent: readers, matching, links, policy, verdict, plan, graph, timeline, what-if, batch | Done |
| Local web app: dashboard, sources, checks, graph explorer, timeline, plan, what-if, batch, policy, benchmark, settings | Done; driven end to end in headless Chromium with no script errors and no outside requests |
| Demo data (`examples/`, fictional) | Done |
| Tests | 73 passing: 22 benchmark-engine, 15 graph/timeline/what-if, 36 general agent, index and server |
| Large data | Measured on a fictional 5-million-row database; see the README's "Large data" section |
| Gemini live use | Not exercised: no key in the build environment. The local rule reader is used and labelled. |
| Hackathon evaluation/usage reports (`code/evaluation/*.md`) | Out of date (from `0318d88`); left untouched |

## Known limitations

- Only supplied files are seen. A "no trace" verdict is limited to the readable sources.
- Live database servers are not queried; their tables must be exported first. The index needs about four times the size of a table export in disk space.
- Kinds of data are guessed from names and can be corrected per file. Sync destinations come from configuration or names. Backups are not itemized.
- OCR mistakes become possible matches, not proof.
- The benchmark engine still leaves 123 evaluation rows without an account step. Deletion-worker events are tied between requests in the same batch and carry no person.
