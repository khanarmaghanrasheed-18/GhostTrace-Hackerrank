# GhostTrace core audit engine

## Scope and contract

GhostTrace audits the 518 requests in `dataset/deletion_requests.csv` using only participant-facing files in `dataset/`. A request that does not authorize deletion is `NOT_APPLICABLE`. For an authorized request, the engine resolves current and historical identifiers, reconstructs the state at `audit_at`, treats workflow results as claims, and applies `dataset/deletion_rules.csv`, including retention and account tombstones.

The root `output.csv` must have exactly `case_id,deletion_status,steps_json`, one row per request sorted by `case_id`. Steps have the six fields specified in `AGENTS.md`, consecutive numbers, and phase order `FINDING`, `REMEDIATION`, `VERIFICATION`. Public answers in `dataset/sample_output.csv` are read only by evaluation code, never by prediction code.

## Architecture

1. **Ingestion:** Parse CSV, JSON, and SQLite-compatible SQL directly. Extract embedded PDF text before OCR. Use OCR for image-only evidence. Cache extraction by file digest. Preserve source path, `file_id`, capture time, observation time, extraction method, and errors. An extraction failure or empty search is not proof that a record is absent.
2. **Identity and investigation:** Build identity links from the current snapshot and historical audit rows. Match records by stable user ID and exact normalized identifiers. Keep every observation with its own provenance, including conflicting primary and replica observations. Exclude evidence captured after `audit_at`; recorded event time and evidence capture time remain distinct.
3. **Policy and output:** Classify record states and active data flows, apply the supplied rules, and generate allowed findings, safe remediation, and verification. Validate the final CSV independently.
4. **Selective model use:** Gemini may help with genuinely ambiguous request language or difficult text interpretation. Its responses must be parsed and validated; Python owns identity matching, evidence search, policy checks, ordering, and final serialization. Record actual calls, retries, token usage, errors, and cached responses. A model result never becomes evidence on its own.
5. **Evaluation:** Run the public sample requests through the same engine; compare with sample answers only in the evaluator. Report status, step, field, and sequence metrics, then add at least three independent cases. Run all 518 evaluation requests and record final usage truthfully.

An in-memory relationship index will connect people, identifiers, records, processes, and evidence. This supports future evidence graph and timeline commands. A later what-if command will apply proposed actions to a temporary case state without changing the audit or evidence. A separate graph database, MCP server, UI, and deployment are deferred.

## Sequence and current status

- Completed: imported and verified the official starter, preserved the original GhostTrace README, inspected all participant-facing formats, and confirmed the cloud shell and local extraction tools.
- Implemented in this milestone: participant evidence ingestion with digest-based cache, identity lookup, time filtering, policy state classification, CSV generation, structural and citation-time validation, terminal inspection, public-sample evaluation, and an optional quota-limited Gemini adapter. The engine keeps observations and provenance; the graph, timeline display, and what-if analysis are still future work.
- Measured current result: `python3 code/main.py run` produced 518 rows and the validator found zero structural/citation-time errors. Evidence extraction covered 94 files with zero reported extraction failures. The public samples yielded 27/32 correct statuses and 5/32 exact step sequences. Thirty-five full-dataset cases were flagged uncertain; 33 authorized rows have no supported steps, so the output is provisional and not ready to submit.
- Planned next: evidence graph and timeline presentation, then isolated what-if cleanup analysis.
- Blocked for live Gemini use: no `GEMINI_API_KEY` binding is present and the last credential-free request to `generativelanguage.googleapis.com` failed with proxy `CONNECT 403`. The current restricted policy does not list that host. The local `gemini-check` therefore made zero calls and returned "not configured". Keep local deterministic work independent; configure the key and destination through cloud environment settings before a real model call.

## Known limitations and next checks

- Matching a current anonymized or absent record back to a person may require a supported historical resource link that this core does not yet recover. The engine refuses to treat an empty or failed search as proof of absence; some rows consequently have no supported steps.
- OCR and extracted text can be wrong. Every observation retains its method and location, but model review hints are not allowed to prove a finding by themselves.
- A successful operation result in `system_activity.csv` is never accepted as the resulting state. Cross-store conflicts remain in the observation index; the case summary flags different observed states for a resource.
- Compare the unresolved sample cases and improve grounded links without using sample answers in prediction logic. Re-run the sample evaluator and full CSV validator after each substantive change. A final submission also needs complete evaluation and usage reports, including costs for any actual model calls.

Run from the repository root. Use environment variables for credentials; do not add them to source, logs, or artifacts. The exact setup and run commands will be recorded in the README once tested.
