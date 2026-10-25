# GhostTrace plan

The authoritative architecture is the "Agreed Architecture" section of [`GHOSTTRACE_PLANNING_CONTEXT.md`](./GHOSTTRACE_PLANNING_CONTEXT.md). This file records how the code implements it and where each milestone stands. The official contract in `AGENTS.md` and `problem_statement.md` governs the submission.

## Scope and contract

GhostTrace audits the 518 requests in `dataset/deletion_requests.csv` using only participant-facing files in `dataset/`.
- A request that does not authorize deletion is `NOT_APPLICABLE`.
- For an authorized request, the engine:
  - resolves current and historical identifiers;
  - reconstructs each record's state at `audit_at`;
  - treats workflow results as claims;
  - applies `dataset/deletion_rules.csv`, including retention and account tombstones.

The root `output.csv` has exactly `case_id,deletion_status,steps_json`, one row per request, sorted by `case_id`. Public sample answers are read only by `code/evaluation/main.py`.

## Architecture (as implemented)

| Module | Responsibility |
| --- | --- |
| `ingest.py` | Parses every evidence file into observations: one resource, one store, one time, one state, with provenance. Handles SQL primary/replica stores, JSON exports, bundles and manifests, eight log syntaxes with trace context, handover rows, PDF text and OCR. Results are cached by file digest in `.cache/ghosttrace/`. |
| `identity.py` | Current and historical identifiers from `user_identities.csv` and `user_audit_log.csv`. |
| `graph.py` | The evidence graph (below). The engine reads identity terms and record links from it. |
| `engine.py` | Intent, workflow attribution, per-store reconstruction at `audit_at`, policy decisions, ordered steps. |
| `views.py` | Per-case graph view, recreation paths and timeline at `audit_at`. |
| `whatif.py` | Hypothetical cleanup analysis on a temporary copy of a case. |
| `gemini.py` | Optional model calls: ambiguous intent, what-if question translation, optional rewording. Every answer is validated by code. |

### Evidence graph

- **Nodes:** people, identifiers (email, phone, email hash), records, stored copies (`record@store`), processes (data flows and backup restore paths), systems, evidence (file, activity or audit IDs), and requests.
- **Edges:** every edge carries `kind`, `evidence_id`, an event or observation time, and a capture time. `kind` is one of:
  - `observed`: identity tables, and record and snapshot contents;
  - `claimed`: activity-stream rows and data-catalog links;
  - `derived`: workflow attribution, inferred absence, a connector destination read from its name, restore lineage.
- **Linking rule:** only exact normalized identifiers or stable user references connect a person to a record. Names and look-alike addresses never create an edge.
- **Identity traversal:** request email → person (current or historical edge) → every identifier the person held → records and processes. The case investigation uses exactly these links: the output was byte-identical before and after the engine switched to the graph.
- **Recreation path:** an enabled connector or restorable archive tied to the person, observed open at `audit_at`. For a connector, the reachable records are those in its destination system, where the connector name states it; otherwise the destination is reported as unknown. For an archive, every archived copy is treated as at risk.

### Timeline

- Lists request milestones, identity changes (audit rows), activity rows (reported claims), and observed states.
- Shows event or observation time and evidence capture time in separate columns.
- Hides entries after `audit_at` unless `--all` is given; it counts what it hides.
- Orders entries by time only and never infers a cause.

### What-if cleanup analysis

1. **Proposals.** Structured `ACTION RESOURCE` pairs, or a natural-language question:
   - With `GEMINI_API_KEY` set, Gemini translates the question into proposed actions using the case's real IDs.
   - Otherwise a labelled local rule reader does the translation.
2. **Validation.** The same code checks every proposal:
   - unknown actions, IDs absent from the evidence, IDs not linked to this person, and action/type mismatches are rejected;
   - an unspecified or ambiguous target becomes a clarification question listing the candidates.
3. **Application.** Accepted actions go into a stored scenario (`.cache/ghosttrace/whatif/<case>.json`). Proposals accumulate across commands; `--reset` clears them. The scenario is applied to a deep copy of the engine's case result.
4. **Re-check.** The copy is checked for:
   - records still exposed or undeleted;
   - weaker-than-required treatment;
   - retention and redaction conflicts (for example, deleting a retained audit record or an archive);
   - recreation paths still open;
   - changes ordered before the process that could undo them;
   - records needing verification after cleanup.
5. **Output.** Every result is labelled hypothetical and limited to the supplied evidence. The explanation is generated from the checked result. `--reword` makes an optional second Gemini call that rewords it without changing facts. Nothing writes to `dataset/`, the audit result, or `output.csv`, and nothing contacts a live system.

## Commands

Run from the repository root. Python 3.12+ standard library; `pdftotext`/`pdftoppm` (poppler-utils) and `tesseract` (tesseract-ocr) for documents and screenshots.

```bash
python3 code/main.py run                 # write output.csv (518 rows)
python3 code/main.py validate
python3 code/main.py inspect case_29     # sample case IDs are found automatically
python3 code/main.py graph case_29       # --json, --all
python3 code/main.py timeline case_29    # --json, --all
python3 code/main.py whatif case_29 --reset --add "DELETE_RECORD search_1993"
python3 code/main.py whatif case_29 --add "DISABLE_DATA_FLOW data_flow_3827"     # cumulative
python3 code/main.py whatif case_33 --ask "What if I delete the export?"         # clarification
python3 code/main.py gemini-check
python3 code/evaluation/main.py          # public-sample metrics
python3 -m unittest discover -s code/evaluation -p 'test_*.py' -v
```

## Milestone status

| Milestone | Status |
| --- | --- |
| 1. Starter import, logging, planning provenance (`docs/GHOSTTRACE_PLANNING_CONTEXT.md`) | Completed |
| 2. Core engine | Completed at `0318d88` |
| 2b. Core correction (log syntaxes and trace context, nested identifiers, catalog/document links, workflow attribution, per-store reconstruction, earlier-cycle and later-only exclusion, policy categories, multilingual and negated intent) | Completed. Samples: status 32/32, exact rows 21/32, step precision 1.000, recall 0.938. 518 rows valid. |
| 3. Evidence graph, timeline, what-if | Completed in this milestone; 15 feature tests |
| 4. Final evaluation and reports | Not started (awaiting review) |
| 5. Full run usage report and packaging | Not started |
| Deferred | UI, deployment, MCP server, separate graph database |

## Known limitations

- Deletion-workflow events carry no person. When several requests share a batch time and reported outcome, an event stays unassigned rather than guessed. This leaves 123 authorized evaluation rows without an account step.
- Some sample-answer resources appear in no participant file, so no method here can find them.
- A file captured after `audit_at` is used only to know that a record existed by `audit_at`, and only when the file itself records an observation time at or before `audit_at`. It is never cited and never decides a state.
- Connector destinations come from connector names. A name that names no system leaves the destination unknown. Archive contents are not itemized, so every archived copy counts as at risk.
- The what-if model covers only records the evidence ties to the person. It shows what the supplied evidence implies, not proof about real systems.
- Gemini has not been called: `GEMINI_API_KEY` is not set in this environment. Earlier, a credential-free request to `generativelanguage.googleapis.com` was refused by the proxy. All 550 request texts are decided by the deterministic intent rules. Natural-language what-if questions currently use the labelled local reader.
