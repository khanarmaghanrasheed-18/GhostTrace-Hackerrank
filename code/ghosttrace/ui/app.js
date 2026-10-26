/* GhostTrace local UI. Talks only to the local server (same origin). */
"use strict";

const h = window.ghtml;
const view = document.getElementById("view");
const STATUS_CLASS = {STILL_EXISTS: "bad", NEEDS_REVIEW: "warn", DELETED: "good", NO_TRACE: "good",
                      INCOMPLETE: "bad", COMPLETE: "good", NOT_APPLICABLE: "neutral"};
const STATUS_TEXT = {STILL_EXISTS: "Still exists", NEEDS_REVIEW: "Needs review", DELETED: "Deleted", NO_TRACE: "No trace found",
                     INCOMPLETE: "Incomplete", COMPLETE: "Complete", NOT_APPLICABLE: "Not a deletion request"};
const ACTION_FOR = {ABSENT: "DELETE_RECORD", REDACTED: "REDACT_RECORD", ANONYMIZED: "ANONYMIZE_RECORD",
                    DISABLED: "DISABLE_DATA_FLOW", BLOCKED: "BLOCK_RESTORE", PRESENT: ""};
const ACTIONS = ["DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE"];
let STATUS = null;

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
const post = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const postRaw = (path, blob) => api(path, {method: "POST", body: blob});

function badge(status) { return h("span", {class: `badge ${STATUS_CLASS[status] || "neutral"}`, text: STATUS_TEXT[status] || status}); }
function card(title, value, note, cls) {
  return h("div", {class: `card ${cls || ""}`}, [h("div", {class: "card-title", text: title}), h("div", {class: "card-value", text: String(value)}),
    note ? h("div", {class: "card-note", text: note}) : null]);
}
function table(headers, rows, cls) {
  return h("div", {class: "tablewrap"}, [h("table", {class: cls || "grid"}, [
    h("thead", {}, [h("tr", {}, headers.map(x => h("th", {text: x})))]),
    h("tbody", {}, rows.map(r => r instanceof Node ? r : h("tr", {}, r.map(c => c instanceof Node ? h("td", {}, [c]) : h("td", {text: c == null ? "" : String(c)})))))])]);
}
function section(title, children, note) {
  return h("section", {class: "panel"}, [h("h2", {text: title}), note ? h("p", {class: "muted", text: note}) : null, ...children]);
}
function errorBox(err) { return h("div", {class: "notice bad", text: `Problem: ${err.message || err}`}); }
function lines(text) { return text.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean); }
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], {type: type || "text/plain"}));
  const a = h("a", {href: url, download: name}); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function bars(title, counts, cls) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(e => e[1]));
  return h("div", {class: "bars"}, [h("h3", {text: title}), ...(entries.length ? entries.map(([k, v]) => {
    const fill = h("div", {class: `bar-fill ${cls || ""}`});
    fill.style.width = `${Math.round(v / max * 100)}%`;
    return h("div", {class: "bar-row"}, [h("span", {class: "bar-label", text: k}), h("div", {class: "bar"}, [fill]), h("span", {class: "bar-value", text: String(v)})]);
  }) : [h("p", {class: "muted", text: "Nothing yet."})])]);
}
function tabs(items) {
  const bar = h("div", {class: "tabs", role: "tablist"});
  const body = h("div", {class: "tabbody"});
  const open = (index) => {
    [...bar.children].forEach((b, i) => b.classList.toggle("active", i === index));
    body.textContent = "";
    try { body.append(items[index][1]()); } catch (err) { body.append(errorBox(err)); }
  };
  items.forEach(([label], i) => bar.append(h("button", {type: "button", role: "tab", text: label, onclick: () => open(i)})));
  const wrap = h("div", {}, [bar, body]);
  setTimeout(() => open(0));
  return wrap;
}

async function refreshStatus() {
  STATUS = await api("/api/status");
  const badgeBox = document.getElementById("privacy");
  badgeBox.textContent = "";
  const ai = STATUS.ai.allowed && STATUS.ai.key_configured;
  badgeBox.append(h("span", {class: `privacy ${ai ? "warn" : "good"}`,
    text: ai ? "AI helper on: excerpts may be sent to Gemini" : "Local only: no data leaves this machine"}));
}

// ---------------------------------------------------------------------------
// Pages

async function dashboard() {
  await refreshStatus();
  const s = STATUS;
  const checks = s.recent_checks;
  const categories = {};
  for (const c of checks) for (const cat of c.categories) categories[cat] = (categories[cat] || 0) + 1;
  const statusCounts = {};
  for (const [k, v] of Object.entries(s.checks_by_status)) statusCounts[STATUS_TEXT[k] || k] = v;
  const start = s.sources === 0 ? section("Get started", [h("ol", {class: "steps"}, [
    h("li", {}, [h("a", {href: "#/sources", text: "Add files"}), h("span", {text: " — spreadsheets, databases, PDFs, screenshots, logs, exports."})]),
    h("li", {}, [h("a", {href: "#/check", text: "Run a check"}), h("span", {text: " for one person by email, phone, name or ID."})]),
    h("li", {}, [h("span", {text: "Explore the graph, the timeline and the cleanup plan, and test cleanup ideas with what-if."})]),
  ])]) : null;
  return h("div", {}, [
    h("h1", {text: "Dashboard"}),
    h("div", {class: "cards"}, [
      card("Sources", s.sources, `${s.records.toLocaleString()} records read`),
      card("Unreadable files", s.unreadable, s.unreadable ? "see Sources" : "all files readable", s.unreadable ? "warn" : ""),
      card("Checks run", s.checks, ""),
      card("Still exists", s.checks_by_status.STILL_EXISTS || 0, "checks with remaining data", (s.checks_by_status.STILL_EXISTS || 0) ? "bad" : ""),
      card("Needs review", s.checks_by_status.NEEDS_REVIEW || 0, "possible matches or unknown states", (s.checks_by_status.NEEDS_REVIEW || 0) ? "warn" : ""),
      card("Deleted / no trace", (s.checks_by_status.DELETED || 0) + (s.checks_by_status.NO_TRACE || 0), "", "good"),
    ]),
    start,
    h("div", {class: "twocol"}, [
      section("Check results", [bars("Checks by verdict", statusCounts)]),
      section("Where data remains", [bars("Kinds of data still found (recent checks)", categories, "bad")]),
    ]),
    section("Recent checks", [checks.length ? table(["When", "Who", "Verdict", "Still exists", "Review", ""], checks.map(c => [
      c.created_at, [...(c.request.emails || []), ...(c.request.phones || []), ...(c.request.ids || []), ...(c.request.names || [])].join(", "),
      badge(c.status), c.counts.still_exists, c.counts.needs_review, h("a", {href: `#/result/${c.id}`, text: "Open"})])) :
      h("p", {class: "muted", text: "No checks yet."})]),
  ]);
}

async function sourcesPage() {
  const list = await api("/api/sources");
  await refreshStatus();
  const status = h("div", {class: "upload-status"});
  const input = h("input", {type: "file", multiple: "multiple", id: "files"});
  const upload = async (files) => {
    for (const file of files) {
      status.textContent = `Reading ${file.name}…`;
      try { await postRaw(`/api/sources?name=${encodeURIComponent(file.name)}`, file); }
      catch (err) { status.append(errorBox(err)); return; }
    }
    route();
  };
  input.addEventListener("change", () => upload([...input.files]));
  const drop = h("label", {class: "dropzone", for: "files"}, [
    h("strong", {text: "Drop files here or click to choose"}),
    h("span", {class: "muted", text: `Supported: ${STATUS.supported.join(" ")}`}), input]);
  drop.addEventListener("dragover", ev => { ev.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", ev => { ev.preventDefault(); drop.classList.remove("over"); upload([...ev.dataTransfer.files]); });
  const manifest = h("input", {type: "file", accept: ".csv"});
  manifest.addEventListener("change", async () => {
    const result = await postRaw("/api/manifest", manifest.files[0]);
    status.textContent = `Capture times updated for ${result.updated} files.`;
    setTimeout(route, 800);
  });
  const rows = list.map(s => {
    const settings = s.settings || {};
    const save = (changes) => post("/api/sources/settings", {name: s.name, changes}).catch(err => alert(err.message));
    const when = h("input", {type: "text", value: settings.captured_at || "", placeholder: s.captured_at, class: "narrow"});
    when.addEventListener("change", () => save({captured_at: when.value}));
    const category = h("select", {}, [h("option", {value: "", text: "auto-detect"}),
      ...STATUS.categories.map(c => h("option", {value: c, text: c}))]);
    category.value = settings.category || "";
    category.addEventListener("change", () => save({category: category.value}));
    const store = h("input", {type: "text", value: settings.store || "", placeholder: "same as file", class: "narrow"});
    store.addEventListener("change", () => save({store: store.value}));
    const complete = h("input", {type: "checkbox"});
    complete.checked = Boolean(settings.complete);
    complete.addEventListener("change", () => save({complete: complete.checked}));
    const remove = h("button", {type: "button", class: "danger", text: "Remove", onclick: async () => {
      if (confirm(`Remove ${s.name} from the workspace?`)) { await post("/api/sources/delete", {name: s.name}); route(); }}});
    const problems = [...s.errors.map(e => h("div", {class: "bad small", text: e})), ...s.notes.map(n => h("div", {class: "muted small", text: n}))];
    return h("tr", {}, [h("td", {text: s.name}), h("td", {text: s.kind}), h("td", {text: s.records.toLocaleString()}),
      h("td", {}, [when]), h("td", {}, [category]), h("td", {}, [store]), h("td", {}, [complete]),
      h("td", {}, problems), h("td", {}, [remove])]);
  });
  return h("div", {}, [
    h("h1", {text: "Sources"}),
    section("Add files", [drop, status, h("p", {class: "small", text: "Files are copied into the local workspace and never uploaded anywhere."}),
      h("details", {}, [h("summary", {text: "Import capture times from a manifest CSV"}),
        h("p", {class: "small muted", text: "A CSV with a file_path (or file/name) column and a captured_at column. Capture time decides which files count for a check 'as of' a date; without it the file's modified time is used."}),
        manifest])]),
    section(`Files in the workspace (${list.length})`, [list.length ? table(
      ["File", "Kind", "Records", "Captured at", "Kind of data", "Store", "Complete snapshot", "Notes", ""], rows) :
      h("p", {class: "muted", text: "No files yet."})],
      "Kind of data picks the policy rule; leave it on auto-detect unless the guess is wrong. Give several files the same Store to treat them as copies of one system over time. Tick Complete snapshot when a file lists every record of that store, so a missing record proves it was removed."),
  ]);
}

function checkForm(prefill) {
  const p = prefill || {};
  const field = (label, name, help, value) => {
    const area = h("textarea", {name, rows: "2", placeholder: help});
    area.value = (value || []).join("\n");
    return h("label", {class: "field"}, [h("span", {text: label}), area]);
  };
  const asOf = h("input", {type: "text", name: "as_of", placeholder: "optional, e.g. 2026-09-15T12:00:00Z", value: p.as_of || ""});
  const label = h("input", {type: "text", name: "label", placeholder: "optional reference, e.g. ticket number", value: p.label || ""});
  const message = h("div", {});
  const form = h("form", {class: "checkform"}, [
    field("Email addresses", "emails", "one per line", p.emails), field("Phone numbers", "phones", "any format", p.phones),
    field("Names", "names", "used only for possible matches", p.names), field("IDs", "ids", "user ID, customer number, account number…", p.ids),
    h("label", {class: "field"}, [h("span", {text: "Check as of"}), asOf]),
    h("label", {class: "field"}, [h("span", {text: "Reference"}), label]),
    h("button", {type: "submit", class: "primary", text: "Run check"}), message]);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const data = new FormData(form);
    message.textContent = "Checking every source…";
    try {
      const result = await post("/api/check", {emails: lines(data.get("emails")), phones: lines(data.get("phones")),
        names: lines(data.get("names")), ids: lines(data.get("ids")), as_of: data.get("as_of"), label: data.get("label"),
        confirmed: p.confirmed || [], rejected: p.rejected || []});
      location.hash = `#/result/${result.id}`;
    } catch (err) { message.textContent = ""; message.append(errorBox(err)); }
  });
  return form;
}

async function checkPage() {
  await refreshStatus();
  return h("div", {}, [h("h1", {text: "New check"}),
    STATUS.sources ? null : h("div", {class: "notice warn", text: "There are no files yet. Add some on the Sources page first."}),
    section("Who should be checked?", [checkForm()], "Enter what you know. GhostTrace also follows links it can prove (for example the user ID on a matched profile, or an old email in an identity-change log) and suggests weaker links for you to confirm.")]);
}

function findingRow(f) {
  const detail = h("tr", {class: "detail hidden"}, [h("td", {colspan: "7"}, [
    h("div", {class: "twocol"}, [
      h("div", {}, [h("h4", {text: "Why it matched"}), h("ul", {}, f.matches.map(m => h("li", {text: `${m.identifier} in ${m.column}: ${m.how}${m.strength === "weak" ? " (possible match)" : ""}`}))),
        h("h4", {text: "Decision"}), h("ul", {}, [
          h("li", {text: `Kind of data: ${f.category} (${f.category_reason})`}),
          f.claim ? null : h("li", {text: `Policy: ${f.rule_id} ${f.policy} → must be ${f.required_state}`}),
          h("li", {text: `State: ${f.state} — ${f.state_reason}`}),
          h("li", {text: `Still identifies the person: ${f.identifying ? "yes" : "no"} ${f.identifying_values.join(", ")}`}),
          f.superseded_by ? h("li", {text: `Superseded by the later observation ${f.superseded_by}`}) : null,
          f.absent_in ? h("li", {text: `Missing from the complete snapshot ${f.absent_in.source}`}) : null,
          h("li", {text: `Observed ${f.observed_at}; source captured ${f.captured_at}; read by ${f.method}${f.method.includes("ocr") ? ` (confidence ${Math.round(f.confidence * 100)}%)` : ""}`})])]),
      h("div", {}, [h("h4", {text: "Record"}), table(["Field", "Value"], Object.entries(f.fields).map(([k, v]) => [k, v]), "kv"),
        f.excerpt && !Object.keys(f.fields).length ? h("pre", {class: "excerpt", text: f.excerpt}) : null]),
    ])])]);
  const row = h("tr", {class: `clickable verdict-${f.verdict.replace(/\s+/g, "-")}`}, [
    h("td", {text: f.id}), h("td", {}, [h("span", {class: `pill v-${f.verdict.replace(/\s+/g, "-")}`, text: f.verdict})]),
    h("td", {text: f.category}), h("td", {text: `${f.source} / ${f.location}`}), h("td", {text: f.state}),
    h("td", {text: f.required_state}), h("td", {text: f.matches.map(m => m.identifier.split(":").slice(1).join(":")).join(", ")})]);
  row.addEventListener("click", () => detail.classList.toggle("hidden"));
  return [row, detail];
}

function findingsTab(result) {
  const verdict = h("select", {}, ["all", "violation", "compliant", "possible match", "unknown", "claim", "history"].map(v => h("option", {value: v, text: v})));
  const search = h("input", {type: "search", placeholder: "filter by file, kind or identifier"});
  const body = h("div");
  const draw = () => {
    const q = search.value.toLowerCase();
    const shown = result.findings.filter(f => (verdict.value === "all" || f.verdict === verdict.value) &&
      (!q || JSON.stringify([f.source, f.location, f.category, f.matches]).toLowerCase().includes(q)));
    body.textContent = "";
    body.append(shown.length ? table(["ID", "Verdict", "Kind of data", "Where", "State", "Policy needs", "Matched"], shown.flatMap(findingRow)) :
      h("p", {class: "muted", text: "Nothing matches the filter."}));
  };
  verdict.addEventListener("change", draw); search.addEventListener("input", draw);
  draw();
  return h("div", {}, [h("div", {class: "toolbar"}, [verdict, search, h("span", {class: "muted small", text: "Click a row for the evidence."})]), body]);
}

function identifiersTab(saved) {
  const result = saved.result;
  const chosen = new Map();
  const choose = (key, value) => chosen.set(key, value);
  const rows = result.subject.map(i => [i.kind, i.value, i.origin, i.how, i.evidence]);
  const suggestions = result.suggested_identifiers.map(i => {
    const yes = h("button", {type: "button", text: "Same person", onclick: () => { choose(i.key, "confirmed"); yes.classList.add("active"); no.classList.remove("active"); }});
    const no = h("button", {type: "button", text: "Not them", onclick: () => { choose(i.key, "rejected"); no.classList.add("active"); yes.classList.remove("active"); }});
    return h("tr", {}, [h("td", {text: i.kind}), h("td", {text: i.value}), h("td", {text: i.how}), h("td", {text: i.evidence}), h("td", {}, [yes, no])]);
  });
  const rerun = h("button", {type: "button", class: "primary", text: "Run again with these answers", onclick: async () => {
    const req = saved.request;
    const confirmed = [...(req.confirmed || []), ...[...chosen].filter(([, v]) => v === "confirmed").map(([k]) => k)];
    const rejected = [...(req.rejected || []), ...[...chosen].filter(([, v]) => v === "rejected").map(([k]) => k)];
    const again = await post("/api/check", {...req, confirmed, rejected});
    location.hash = `#/result/${again.id}`;
  }});
  return h("div", {}, [
    h("h3", {text: "Identifiers used"}), table(["Kind", "Value", "Origin", "How it is known", "Found in"], rows),
    h("h3", {text: `Suggested links (${suggestions.length})`}),
    suggestions.length ? h("div", {}, [h("p", {class: "muted", text: "These values sit next to the person's data but could belong to someone else. Confirm only what you know."}),
      table(["Kind", "Value", "Why suggested", "Found in", "Decision"], suggestions), rerun]) :
      h("p", {class: "muted", text: "No suggestions."}),
  ]);
}

function timelineTab(entries) {
  return entries.length ? table(["Time", "Type", "Item", "Captured", "What"], entries.map(e => [e.time, e.type, e.source, e.captured_at || "-", e.what])) :
    h("p", {class: "muted", text: "No dated items."});
}

function planTab(plan, label) {
  if (!plan.length) return h("p", {class: "muted", text: "No steps: nothing about this person was found."});
  return h("div", {}, [label ? h("p", {class: "muted", text: label}) : null,
    table(["#", "Phase", "Operation", "State", "Item", "Where", "Proof"], plan.map(s => [s.step_no, s.phase, s.operation, s.state,
      s.resource_id, s.where || s.system_id, s.evidence_id || "—"]))]);
}

function whatifPanel(options) {
  // options: {items: [{id, label, suggested}], run: async (actions, question) => response}
  let actions = [];
  const list = h("ul", {class: "proposals"});
  const output = h("div", {class: "whatif-output"});
  const question = h("input", {type: "text", placeholder: "e.g. What if I delete the search entries and disable the connector?"});
  const draw = (response) => {
    list.textContent = "";
    actions.forEach((a, i) => list.append(h("li", {}, [h("span", {text: `${a.action} ${a.target}`}),
      h("button", {type: "button", class: "small", text: "remove", onclick: () => { actions.splice(i, 1); run(); }})])));
    if (!actions.length) list.append(h("li", {class: "muted", text: "No proposed actions yet."}));
    output.textContent = "";
    if (!response) return;
    const notes = [];
    for (const m of response.messages || []) notes.push(h("div", {class: "notice warn", text: `${m.status}: ${m.action} ${m.target || ""} — ${m.reason || m.question || ""}`}));
    const i = response.interpretation;
    if (i) {
      notes.push(h("div", {class: "small muted", text: `Interpreted by ${i.interpreter}`}));
      for (const c of i.clarify) notes.push(h("div", {class: "notice warn", text: `Clarification needed: ${c.question}`}));
      for (const r of i.rejected) notes.push(h("div", {class: "notice bad", text: `Rejected: ${r.action} ${r.target || r.resource_id || ""} — ${r.reason}`}));
    }
    const verdict = response.assessment.would_be || response.assessment.hypothetical_status;
    output.append(...notes, h("div", {class: "hypothetical"}, [h("strong", {text: "Hypothetical result: "}), badge(verdict)]),
      h("pre", {class: "explanation", text: response.explanation}));
  };
  const run = async (text) => {
    try {
      const response = await options.run(actions, text || "");
      actions = response.actions;
      draw(response);
    } catch (err) { output.textContent = ""; output.append(errorBox(err)); }
  };
  const rows = options.items.map(item => {
    const select = h("select", {}, [h("option", {value: "", text: item.suggested ? "choose…" : "keep (policy retains it)"}),
      ...ACTIONS.map(a => h("option", {value: a, text: a}))]);
    select.value = item.suggested || "";
    return h("tr", {}, [h("td", {text: item.id}), h("td", {text: item.label}), h("td", {}, [select]),
      h("td", {}, [h("button", {type: "button", text: "Add", onclick: () => {
        if (!select.value) return;
        actions = actions.filter(a => a.target !== item.id).concat([{action: select.value, target: item.id}]); run(); }})])]);
  });
  const ask = h("button", {type: "button", class: "primary", text: "Ask", onclick: () => run(question.value)});
  question.addEventListener("keydown", ev => { if (ev.key === "Enter") run(question.value); });
  draw(null);
  return h("div", {class: "whatif"}, [
    h("div", {class: "notice neutral", text: "Hypothetical only: proposals are tested on a temporary copy. Nothing is deleted, the saved result does not change, and no system is contacted."}),
    h("div", {class: "twocol"}, [
      h("div", {}, [h("h3", {text: "Propose actions"}), table(["Item", "What", "Action", ""], rows),
        h("h3", {text: "Or ask a question"}), h("div", {class: "toolbar"}, [question, ask])]),
      h("div", {}, [h("h3", {text: "Current proposal (cumulative)"}), list,
        h("button", {type: "button", text: "Reset scenario", onclick: () => { actions = []; run(); }}), output]),
    ]),
  ]);
}

async function resultPage(id) {
  const saved = await api(`/api/checks/${id}`);
  const r = saved.result;
  const who = [...(saved.request.emails || []), ...(saved.request.phones || []), ...(saved.request.ids || []), ...(saved.request.names || [])].join(", ");
  const remaining = r.findings.filter(f => f.verdict === "violation");
  const current = r.findings.filter(f => !["history", "possible match", "claim"].includes(f.verdict));
  return h("div", {}, [
    h("h1", {text: `Check: ${who}`}),
    h("div", {class: `verdict ${STATUS_CLASS[r.status]}`}, [h("div", {class: "verdict-title", text: r.status_text}),
      h("div", {class: "small", text: `${r.label}${r.as_of ? " As of " + r.as_of + "." : ""} ${saved.request.label ? "Reference: " + saved.request.label : ""}`})]),
    h("div", {class: "cards"}, [card("Still exists", r.counts.still_exists, "copies that break the policy", r.counts.still_exists ? "bad" : ""),
      card("Compliant", r.counts.compliant, "deleted, redacted or retained as required", "good"),
      card("Needs review", r.counts.needs_review, "possible matches, unknown states, form claims", r.counts.needs_review ? "warn" : ""),
      card("Recreation paths", r.recreation_paths.length, "sync or restore processes that can bring data back", r.recreation_paths.length ? "bad" : ""),
      card("Records scanned", r.counts.records_scanned.toLocaleString(), `${r.counts.sources_used} of ${r.counts.sources} sources`)]),
    h("div", {class: "toolbar"}, [h("a", {class: "button", href: `/api/checks/${id}/export.csv`, text: "Download findings (CSV)"}),
      h("button", {type: "button", text: "Download full result (JSON)", onclick: () => download(`${id}.json`, JSON.stringify(saved, null, 2), "application/json")}),
      h("button", {type: "button", class: "danger", text: "Delete this result", onclick: async () => { if (confirm("Delete this saved result?")) { await post("/api/checks/delete", {id}); location.hash = "#/dashboard"; } }})]),
    tabs([
      ["Where it still exists", () => remaining.length || r.recreation_paths.length ? h("div", {}, [
        table(["ID", "Kind of data", "Where", "Policy needs", "Why"], remaining.map(f => [f.id, f.category, `${f.source} / ${f.location}`, `${f.policy} → ${f.required_state}`, f.state_reason])),
        r.recreation_paths.length ? h("h3", {text: "Processes that can recreate data"}) : null,
        r.recreation_paths.length ? table(["Process", "Kind", "State", "Where", "Can bring back", "Basis"], r.recreation_paths.map(p => [p.process, p.kind, p.state, p.where, p.can_recreate.join(", ") || "—", p.basis])) : null,
      ]) : h("p", {class: "muted", text: "No copy breaks the policy in the readable sources."})],
      ["All findings", () => findingsTab(r)],
      ["Graph", () => { const box = h("div", {class: "graphbox"}); setTimeout(() => new GraphView(box, r.graph)); return box; }],
      ["Timeline", () => timelineTab(r.timeline)],
      ["Cleanup plan", () => planTab(r.plan, "Findings first, then remediation (processes are stopped before the records they feed are changed), then the checks that must pass afterwards.")],
      ["What-if", () => whatifPanel({items: current.map(f => ({id: f.id, label: `${f.category} — ${f.source} / ${f.location}`, suggested: ACTION_FOR[f.required_state]})),
        run: (actions, question) => post("/api/whatif", {check_id: id, actions, question})})],
      ["Identifiers", () => identifiersTab(saved)],
      ["Coverage", () => h("div", {}, [
        r.coverage.unreadable.length ? h("div", {class: "notice warn", text: `Not readable, so not checked: ${r.coverage.unreadable.join(", ")}`}) : null,
        r.coverage.after_as_of.length ? h("div", {class: "notice neutral", text: `Captured after the check time, so not used: ${r.coverage.after_as_of.join(", ")}`}) : null,
        table(["Source", "Kind", "Records", "Captured", "Notes"], r.coverage.sources.map(s => [s.name, s.kind, s.records, s.captured_at, [...s.errors, ...s.notes].join("; ")]))])],
      ["Policy used", () => table(["Rule", "Kind of data", "Condition", "Action"], r.policy.map(p => [p.rule_id, p.data_category, p.condition, p.required_action]))],
    ]),
  ]);
}

async function batchPage() {
  const input = h("input", {type: "file", accept: ".csv"});
  const output = h("div");
  input.addEventListener("change", async () => {
    output.textContent = "Checking each row…";
    try {
      const response = await postRaw("/api/batch", input.files[0]);
      output.textContent = "";
      const counts = {};
      for (const r of response.results) counts[STATUS_TEXT[r.status] || r.status] = (counts[STATUS_TEXT[r.status] || r.status] || 0) + 1;
      output.append(bars("Verdicts", counts),
        h("button", {type: "button", text: "Download results (CSV)", onclick: () => download("ghosttrace_batch.csv", response.csv, "text/csv")}),
        table(["Request", "Verdict", "Still exists", "Review", "Where"], response.results.map(r => [r.request, badge(r.status), r.still_exists, r.needs_review, r.where])));
    } catch (err) { output.textContent = ""; output.append(errorBox(err)); }
  });
  return h("div", {}, [h("h1", {text: "Batch check"}), section("Check many people at once", [input, output],
    "Upload a CSV with one row per person. Recognized columns: email, phone, name, user_id / customer_id / id, as_of, request_id, request_text. Several values in one cell can be separated with ';'. When request_text is present, rows that do not ask for deletion are marked as such.")]);
}

async function policyPage() {
  const data = await api("/api/policy");
  const input = h("input", {type: "file", accept: ".csv"});
  const message = h("div");
  input.addEventListener("change", async () => {
    try { await postRaw("/api/policy", input.files[0]); route(); } catch (err) { message.append(errorBox(err)); }
  });
  return h("div", {}, [h("h1", {text: "Deletion policy"}),
    section(data.custom ? "Custom policy (merged over the default)" : "Default policy", [
      table(["Rule", "Kind of data", "Condition", "Action"], data.rules.map(p => [p.rule_id, p.data_category, p.condition, p.required_action])),
      h("h3", {text: "Use your own rules"}),
      h("p", {class: "small", text: "Upload a CSV with columns rule_id, data_category, condition, required_action (DELETE, REDACT, ANONYMIZE or RETAIN). Categories you list replace the default rule; the rest stay."}),
      input, data.custom ? h("button", {type: "button", text: "Back to the default policy", onclick: async () => { await post("/api/policy/reset", {}); route(); }}) : null, message])]);
}

async function benchmarkPage() {
  const rows = await api("/api/benchmark");
  const filter = h("select", {}, [["all", "All cases"], ["sample", "Public samples (with answers)"], ["eval", "Evaluation requests"]].map(([v, t]) => h("option", {value: v, text: t})));
  const search = h("input", {type: "search", placeholder: "case, email or person"});
  const body = h("div");
  const draw = () => {
    const q = search.value.toLowerCase();
    const shown = rows.filter(r => (filter.value === "all" || (filter.value === "sample") === r.sample) &&
      (!q || `${r.case_id} ${r.email} ${r.person}`.toLowerCase().includes(q)));
    body.textContent = "";
    body.append(table(["Case", "Email", "Person", "Workflow claimed", "Audit at", "GhostTrace verdict", "Steps", ""], shown.slice(0, 600).map(r => [
      r.case_id, r.email, r.person || "—", r.reported_status, r.audit_at, badge(r.status), r.steps, h("a", {href: `#/benchmark/${r.case_id}`, text: "Explore"})])));
  };
  filter.addEventListener("change", draw); search.addEventListener("input", draw); draw();
  const counts = {};
  for (const r of rows) counts[STATUS_TEXT[r.status]] = (counts[STATUS_TEXT[r.status]] || 0) + 1;
  return h("div", {}, [h("h1", {text: "Benchmark: Northstar Shop dataset"}),
    h("p", {class: "muted", text: "The bundled synthetic dataset of 550 deletion requests with screenshots, PDFs, SQL replicas, logs and JSON exports. GhostTrace's audit engine reconstructs each case at its audit time."}),
    section("Verdicts", [bars("Cases by verdict", counts)]), section("Cases", [h("div", {class: "toolbar"}, [filter, search]), body])]);
}

async function benchmarkCase(caseId) {
  const data = await api(`/api/benchmark/${caseId}`);
  const req = data.request;
  const items = data.view.records.map(r => ({id: r.resource_id, label: `${r.category} — ${r.system_id}`, suggested: ACTION_FOR[r.required_state] || ""}));
  return h("div", {}, [h("h1", {text: `Benchmark ${caseId}`}),
    h("div", {class: `verdict ${STATUS_CLASS[data.status]}`}, [h("div", {class: "verdict-title", text: STATUS_TEXT[data.status]}),
      h("div", {class: "small", text: `${req.email} · requested ${req.requested_at} · workflow claimed ${req.reported_status} at ${req.reported_at} · audited at ${req.audit_at}`})]),
    h("p", {class: "quote", text: req.request_text}),
    tabs([
      ["Graph", () => { const box = h("div", {class: "graphbox"}); setTimeout(() => new GraphView(box, data.graph)); return box; }],
      ["Steps (output.csv row)", () => planTab(data.steps)],
      ["Timeline", () => timelineTab(data.timeline.entries.map(e => ({...e, captured_at: e.captured_at})))],
      ["What-if", () => whatifPanel({items, run: (actions, question) => post(`/api/benchmark/${caseId}/whatif`, {actions, question})})],
    ])]);
}

async function settingsPage() {
  await refreshStatus();
  const ai = STATUS.ai;
  const toggle = h("input", {type: "checkbox"});
  toggle.checked = ai.allowed;
  toggle.addEventListener("change", async () => {
    if (toggle.checked && !confirm("With the AI helper on, short excerpts and your what-if questions are sent to Google's Gemini API. Turn it on?")) { toggle.checked = false; return; }
    await post("/api/settings", {ai_allowed: toggle.checked}); route();
  });
  return h("div", {}, [h("h1", {text: "Settings"}),
    section("Privacy", [h("ul", {}, [h("li", {text: "GhostTrace runs only on this computer and answers only on 127.0.0.1."}),
      h("li", {text: "Files, results and settings are kept in the workspace folder:"}), h("li", {}, [h("code", {text: STATUS.workspace})]),
      h("li", {text: "The page loads no outside scripts, fonts or images."})])]),
    section("AI helper (optional)", [
      h("p", {text: ai.key_configured ? "A GEMINI_API_KEY is configured in the environment." : "No GEMINI_API_KEY is configured, so the AI helper cannot be used. Everything else works without it."}),
      h("label", {class: "toggle"}, [toggle, h("span", {text: " Allow sending excerpts and questions to Gemini"})]),
      h("p", {class: "small muted", text: "When allowed, Gemini translates what-if questions into proposed actions. Code still validates every proposal. Without it, a local rule reader is used."}),
      h("p", {class: "small", text: `Model calls this session: ${ai.usage.calls}, tokens in/out: ${ai.usage.input_tokens}/${ai.usage.output_tokens}`})]),
  ]);
}

// ---------------------------------------------------------------------------
// Router

const ROUTES = [
  [/^#\/dashboard$/, dashboard], [/^#\/sources$/, sourcesPage], [/^#\/check$/, checkPage],
  [/^#\/result\/([\w-]+)$/, resultPage], [/^#\/batch$/, batchPage], [/^#\/policy$/, policyPage],
  [/^#\/benchmark$/, benchmarkPage], [/^#\/benchmark\/(case_\d+)$/, benchmarkCase], [/^#\/settings$/, settingsPage],
];

async function route() {
  const hash = location.hash || "#/dashboard";
  for (const link of document.querySelectorAll("#nav a")) link.classList.toggle("active", hash.startsWith(link.getAttribute("href")));
  const match = ROUTES.map(([re, fn]) => [hash.match(re), fn]).find(([m]) => m);
  view.textContent = "";
  view.append(h("p", {class: "muted", text: "Loading…"}));
  try {
    const page = match ? await match[1](...match[0].slice(1)) : await dashboard();
    view.textContent = "";
    view.append(page);
  } catch (err) {
    view.textContent = "";
    view.append(errorBox(err));
  }
}

window.addEventListener("hashchange", route);
route();
