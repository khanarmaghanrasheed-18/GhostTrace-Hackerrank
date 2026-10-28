/* One check: verdict, evidence, graph, timeline, cleanup plan, what-if, identifiers, coverage. */

import {ACTION, api, badge, card, categoryLabel, clear, confirmDialog, download, drawer, emptyState, errorNotice, FINDING,
        findingBadge, h, icon, ID_KIND, modal, n, OPERATION, ORIGIN, post, remember, segmented, stat, table, tabs, toast, VERDICT, when,
        who} from "./core.js";
import {GraphView} from "./graph.js";
import {onLeave} from "./state.js";

const ACTIONS = ["DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE"];
const ACTION_FOR = {ABSENT: "DELETE_RECORD", REDACTED: "REDACT_RECORD", ANONYMIZED: "ANONYMIZE_RECORD",
                    DISABLED: "DISABLE_DATA_FLOW", BLOCKED: "BLOCK_RESTORE", PRESENT: ""};
const REQUIRED = {ABSENT: "gone", REDACTED: "redacted", ANONYMIZED: "anonymized", PRESENT: "kept", DISABLED: "turned off", BLOCKED: "blocked from restoring"};
const STATE = {PRESENT: "Stored", ABSENT: "Gone", ACTIVE: "Running", DISABLED: "Turned off", RESTORABLE: "Can be restored",
               BLOCKED: "Restore blocked", UNKNOWN: "Not established"};
const STATE_PHRASE = {ACTIVE: "is running", RESTORABLE: "can be restored", DISABLED: "is turned off", BLOCKED: "is blocked from restoring"};
const sentence = text => (text ? text.charAt(0).toUpperCase() + text.slice(1) + (/[.!?]$/.test(text) ? "" : ".") : "");
const PHASES = [["FINDING", "What was found", "var(--bad)"], ["REMEDIATION", "What to do", "var(--accent)"],
                ["VERIFICATION", "How to confirm it worked", "var(--good)"]];

export async function resultPage(id, initialTab) {
  const saved = await api(`/api/checks/${id}`);
  const r = saved.result;
  const req = saved.request;
  const findings = r.findings;
  const byId = Object.fromEntries(findings.map(f => [f.id, f]));
  const current = findings.filter(f => f.verdict !== "history");
  const remaining = current.filter(f => f.verdict === "violation");
  const review = current.filter(f => ["possible match", "unknown", "claim"].includes(f.verdict));
  const compliant = current.filter(f => f.verdict === "compliant");
  const v = VERDICT[r.status] || VERDICT.NEEDS_REVIEW;
  const openFinding = fid => showFinding(byId[fid], r);
  let graphView = null;
  onLeave(() => { if (graphView) graphView.destroy(); });

  // -- header --------------------------------------------------------------
  const subject = h("div", {class: "subjectline"}, [
    ...(req.emails || []).map(x => chip("email", x)), ...(req.phones || []).map(x => chip("phone", x)),
    ...(req.ids || []).map(x => chip("id", x)), ...(req.names || []).map(x => chip("name", x))]);
  const rerun = async () => {
    try {
      toast("Running the check again…");
      const again = await post("/api/check", req);
      location.hash = `#/result/${again.id}`;
    } catch (err) { toast(err.message, "bad"); }
  };
  const verdictText = r.status === "STILL_EXISTS"
    ? `${n(remaining.length)} ${remaining.length === 1 ? "copy breaks" : "copies break"} the policy` +
      (r.recreation_paths.length ? ` and ${n(r.recreation_paths.length)} ${r.recreation_paths.length === 1 ? "process" : "processes"} can bring data back.` : ".")
    : v.says;
  const linkedCount = r.subject.filter(i => i.origin === "linked").length;
  const header = h("div", {class: "stack"}, [
    h("a", {class: "crumb", href: req.batch ? "#/batch" : "#/results"}, [icon("arrowLeft", "sm"), req.batch ? `Batch: ${req.batch}` : "All results"]),
    h("div", {class: "pagehead", style: "margin-bottom:0"}, [
      h("div", {class: "stack-sm"}, [subject,
        h("div", {class: "metaline"}, [
          h("span", {}, [icon("clock", "sm"), `Checked ${when(saved.created_at)}`]),
          r.as_of ? h("span", {}, [icon("history", "sm"), `State as of ${when(r.as_of)}`]) : null,
          h("span", {}, [icon("database", "sm"), `${n(r.counts.records_scanned)} records in ${n(r.counts.sources_used)} files`]),
          linkedCount ? h("span", {}, [icon("link", "sm"), `${linkedCount} more identifiers found and followed`]) : null,
          req.label ? h("span", {}, [icon("hash", "sm"), req.label]) : null])]),
      h("div", {class: "row"}, [
        h("a", {class: "btn", href: `/api/checks/${id}/report.html`, target: "_blank", rel: "noopener"}, [icon("printer", "sm"), "Report"]),
        h("button", {class: "btn", type: "button", onclick: () => exportMenu(saved)}, [icon("download", "sm"), "Export"]),
        h("button", {class: "btn", type: "button", onclick: rerun}, [icon("refresh", "sm"), "Run again"]),
        h("button", {class: "btn ghost danger", type: "button", "aria-label": "Delete this result", title: "Delete this result", onclick: async () => {
          if (!await confirmDialog("Delete this result?", "The saved result is removed from this computer. Your files are not touched.", "Delete", true)) return;
          await post("/api/checks/delete", {id});
          toast("Result deleted.");
          location.hash = "#/results";
        }}, [icon("trash", "sm")])])]),
    h("section", {class: `verdict ${v.tone}`, role: "status"}, [h("div", {class: "vicon"}, [icon(v.icon)]),
      h("div", {}, [h("h2", {text: v.title}), h("p", {text: verdictText}), h("p", {class: "next", text: v.next}),
        h("p", {class: "small", style: "opacity:.75", text: r.label})])]),
    h("div", {class: "stats"}, [
      stat("Still exists", n(r.counts.still_exists), "copies that break the policy", r.counts.still_exists ? "bad" : "", "alert"),
      stat("Can come back", n(r.recreation_paths.length), "syncs or backups that can restore data", r.recreation_paths.length ? "bad" : "", "repeat"),
      stat("Needs review", n(r.counts.needs_review), "possible matches, unknowns, claims", r.counts.needs_review ? "warn" : "", "help"),
      stat("As required", n(r.counts.compliant), "deleted, redacted or kept per policy", "good", "checkCircle"),
    ]),
    r.counts.possible_not_listed ? notice("warn", "users", `${n(r.counts.possible_not_listed)} more records match only by name or by a shared phone number and are not listed one by one. Add an email, phone or ID to narrow the check.`) : null,
    r.coverage.unreadable.length ? notice("bad", "alertCircle", `Not checked because they could not be read: ${r.coverage.unreadable.join(", ")}. A clean verdict is not given while files remain unread.`) : null,
    r.suggested_identifiers.length ? h("div", {class: "notice info"}, [icon("link"), h("div", {class: "grow"},
      [`${r.suggested_identifiers.length} more ${r.suggested_identifiers.length === 1 ? "value looks" : "values look"} like this person's, but could belong to someone else. `,
        h("a", {href: `#/result/${id}/identifiers`, text: "Review the suggested links"})])]) : null,
  ]);

  // -- tabs ------------------------------------------------------------------
  const plan = r.plan;
  const tabItems = [
    {key: "summary", label: "Summary", icon: "list", render: () => summaryTab()},
    {key: "findings", label: "All findings", icon: "fileText", count: current.length, render: () => findingsTab(current, openFinding)},
    {key: "graph", label: "Graph", icon: "graph", render: () => {
      const box = h("div");
      setTimeout(() => { if (graphView) graphView.destroy(); graphView = new GraphView(box, r.graph, {onFinding: openFinding}); });
      return box;
    }},
    {key: "timeline", label: "Timeline", icon: "clock", count: r.timeline.length, render: () => timelineTab(r.timeline)},
    {key: "plan", label: "Cleanup plan", icon: "wand", count: plan.length, render: () => planTab(id, plan, byId, openFinding)},
    {key: "whatif", label: "What-if", icon: "sparkle", render: () => whatifPanel(id, current.filter(f => !["possible match", "claim"].includes(f.verdict)))},
    {key: "identifiers", label: "Identifiers", icon: "key", count: r.subject.length + r.suggested_identifiers.length, render: () => identifiersTab(saved)},
    {key: "coverage", label: "Coverage", icon: "database", render: () => coverageTab(r)},
  ];

  function summaryTab() {
    const findList = (items, emptyText) => items.length ? h("div", {}, items.map(f => findCard(f, openFinding))) :
      h("p", {class: "muted", style: "padding:16px", text: emptyText});
    // Grouped by file, so a long list reads as "this database: 6 copies".
    const byFile = [];
    for (const f of remaining) {
      const last = byFile[byFile.length - 1];
      if (last && last.source === f.source) last.items.push(f); else byFile.push({source: f.source, items: [f]});
    }
    const grouped = byFile.length ? h("div", {}, byFile.map(group => h("div", {}, [
      h("div", {class: "row", style: "padding:9px 16px;background:var(--surface-2);border-bottom:1px solid var(--line)"}, [
        icon("file", "sm"), h("strong", {class: "small wrap grow", text: group.source}), h("span", {class: "tiny muted", text: `${group.items.length} ${group.items.length === 1 ? "copy" : "copies"}`})]),
      ...group.items.map(f => findCard(f, openFinding))]))) : h("p", {class: "muted", style: "padding:16px", text: "No copy breaks the policy in the readable files."});
    return h("div", {class: "grid-2"}, [
      h("div", {class: "stack"}, [
        r.recreation_paths.length ? card("What can bring it back", "Stop these first, or the cleanup can be undone.",
          h("div", {}, r.recreation_paths.map(p => h("div", {class: "findcard", onclick: () => openFinding(p.process)}, [
            h("span", {class: "findid", text: p.process}), h("div", {class: "grow"}, [
              h("div", {class: "where", text: p.where}),
              h("div", {class: "why", text: `${p.kind === "sync" ? "A sync" : "A backup"} that ${STATE_PHRASE[p.state] || p.state}. ${p.can_recreate.length ? `It can bring back ${p.can_recreate.join(", ")}.` : ""} ${sentence(p.basis)}`})]),
            h("div", {class: "side"}, [badge("bad", p.kind === "sync" ? "Sync running" : "Restorable")])]))), null, "flush") : null,
        card("Where it still exists", remaining.length ? `${remaining.length} ${remaining.length === 1 ? "copy" : "copies"} in ${byFile.length} ${byFile.length === 1 ? "file" : "files"}, each to be treated as the policy says.` : null,
          grouped, null, "flush")]),
      h("div", {class: "stack"}, [
        review.length ? card("Needs a person to review", "Possible matches and claims are never treated as proof.",
          findList(review, ""), null, "flush") : null,
        compliant.length ? card("Treated as required", "Deleted, redacted, anonymized or kept as the policy says.",
          findList(compliant, ""), null, "flush") : null,
        !review.length && !compliant.length && !remaining.length ? card(null, null, emptyState("shieldCheck", "Nothing found",
          "No record in the readable files holds these identifiers.")) : null]),
    ]);
  }

  return h("div", {class: "stack"}, [header,
    tabs(tabItems, initialTab || remember(`tab.${id}`) || "summary", key => {
      remember(`tab.${id}`, key);
      if (key !== "graph" && graphView) { graphView.destroy(); graphView = null; }
    })]);
}

function chip(kind, value) {
  return h("span", {class: "chip", style: "font-size:14px;padding:5px 12px"}, [icon(ID_KIND[kind].icon, "sm"), h("span", {class: "wrap", text: value})]);
}
function notice(tone, iconName, text) { return h("div", {class: `notice ${tone}`}, [icon(iconName), h("div", {text})]); }

function exportMenu(saved) {
  modal("Export this result", [
    h("p", {class: "muted", text: "Exports are saved by your browser to this computer."}),
    h("div", {class: "stack-sm"}, [
      h("a", {class: "btn", href: `/api/checks/${saved.id}/report.html`, target: "_blank", rel: "noopener"}, [icon("printer", "sm"), "Printable report (HTML, print to PDF)"]),
      h("a", {class: "btn", href: `/api/checks/${saved.id}/export.csv`}, [icon("sheet", "sm"), "Findings as CSV"]),
      h("button", {class: "btn", type: "button", onclick: () => download(`${saved.id}.json`, JSON.stringify(saved, null, 2), "application/json")},
        [icon("braces", "sm"), "Full result as JSON"])])]);
}

// ---------------------------------------------------------------------------
// Findings

function findCard(f, open) {
  const matched = f.matches.map(m => m.identifier.split(":").slice(1).join(":"));
  return h("div", {class: "findcard", role: "button", tabindex: "0", onclick: () => open(f.id),
                   onkeydown: ev => { if (ev.key === "Enter") open(f.id); }}, [
    h("span", {class: "findid", text: f.id}),
    h("div", {class: "grow", style: "min-width:0"}, [
      h("div", {class: "where", text: `${f.source} / ${f.location}`}),
      h("div", {class: "why", text: f.claim ? `The screen says “${f.claim}”. A form's claim, not proof.` : f.state_reason}),
      h("div", {class: "tiny faint", style: "margin-top:4px", text: `Matched by ${matched.slice(0, 3).join(", ")}${matched.length > 3 ? "…" : ""}`})]),
    h("div", {class: "side"}, [findingBadge(f.verdict), h("span", {class: "tiny muted", text: categoryLabel(f.category)}),
      f.policy && f.verdict === "violation" ? h("span", {class: "tiny", text: `Must be ${REQUIRED[f.required_state] || f.required_state}`}) : null])]);
}

function findingsTab(current, open) {
  const verdicts = ["violation", "possible match", "unknown", "claim", "compliant"];
  let filter = "all";
  let query = "";
  const body = h("div");
  const search = h("input", {type: "search", placeholder: "Filter by file, kind or identifier", "aria-label": "Filter findings"});
  const draw = () => {
    const shown = current.filter(f => (filter === "all" || f.verdict === filter) &&
      (!query || JSON.stringify([f.source, f.location, f.category, categoryLabel(f.category), f.matches]).toLowerCase().includes(query)));
    clear(body, shown.length ? table(["", "Status", "Kind of data", "Where", "State", "Policy", "Matched by"], shown.map(f => h("tr", {class: "click", onclick: () => open(f.id)}, [
      h("td", {class: "mono faint", text: f.id}), h("td", {}, [findingBadge(f.verdict)]), h("td", {text: categoryLabel(f.category)}),
      h("td", {class: "wrap", text: `${f.source} / ${f.location}`}), h("td", {class: "small", text: STATE[f.state] || f.state}),
      h("td", {class: "small", text: f.policy ? `${(ACTION[f.policy] || {}).text || f.policy} (${f.rule_id})` : "—"}),
      h("td", {class: "small wrap", text: f.matches.map(m => m.identifier.split(":").slice(1).join(":")).join(", ")})]))) :
      emptyState("filter", "No findings match", "Try another filter."));
  };
  search.addEventListener("input", () => { query = search.value.toLowerCase(); draw(); });
  draw();
  const counts = Object.fromEntries(verdicts.map(v => [v, current.filter(f => f.verdict === v).length]));
  return card(null, null, [
    h("div", {class: "row between", style: "padding:12px 16px;border-bottom:1px solid var(--line)"}, [
      segmented([["all", "All", current.length], ...verdicts.filter(v => counts[v]).map(v => [v, FINDING[v].text, counts[v]])], filter, key => { filter = key; draw(); }),
      h("div", {style: "width:min(300px,100%)"}, [search])]), body], null, "flush");
}

export function showFinding(f, r) {
  if (!f) return;
  const values = new Set(f.matches.map(m => String(m.value).toLowerCase()));
  const fields = Object.entries(f.fields || {});
  const decision = [
    ["Kind of data", `${categoryLabel(f.category)} — ${f.category_reason}`],
    f.policy ? ["Policy", `${f.rule_id} says ${(ACTION[f.policy] || {}).text || f.policy}, so it must be ${REQUIRED[f.required_state] || f.required_state}`] : null,
    ["State", `${STATE[f.state] || f.state} — ${f.state_reason}`],
    ["Identifies the person", f.identifying ? `Yes${f.identifying_values.length ? `: ${f.identifying_values.join(", ")}` : ""}` : "No"],
    ["Observed", `${when(f.observed_at)}${f.captured_at ? ` (file captured ${when(f.captured_at)})` : ""}`],
    ["Read by", `${f.method}${f.method.includes("ocr") ? `, OCR confidence ${Math.round(f.confidence * 100)}%` : ""}`],
    f.superseded_by ? ["Later copy", `A later observation of the same record is ${f.superseded_by}.`] : null,
    f.absent_in ? ["Removed", `Missing from the complete snapshot ${f.absent_in.source}.`] : null,
  ].filter(Boolean);
  const paths = (r.recreation_paths || []).filter(p => p.can_recreate.includes(f.id));
  drawer(`${f.id} · ${categoryLabel(f.category)}`, [findingBadge(f.verdict), " ", h("span", {class: "wrap", text: `${f.source} / ${f.location}`})], [
    f.claim ? notice("warn", "info", `The screen shows “${f.claim}”. That is the form's claim, not proof: a form shows only what its query selects and can hide rows marked deleted. Add the database files behind it to confirm.`) : null,
    paths.length ? notice("bad", "repeat", `${paths.map(p => p.process).join(", ")} can bring this back. Stop ${paths.length === 1 ? "it" : "them"} before changing this record.`) : null,
    h("div", {class: "stack-sm"}, [h("h4", {text: "Why it matched"}), h("ul", {class: "matchlist"}, f.matches.map(m => {
      const kind = m.identifier.split(":")[0];
      return h("li", {}, [icon((ID_KIND[kind] || ID_KIND.id).icon, "sm"), h("div", {}, [h("strong", {class: "wrap", text: m.identifier.split(":").slice(1).join(":")}),
        ` in ${m.column}: ${m.how}`, m.strength === "weak" ? h("span", {class: "badge warn", style: "margin-left:6px", text: "possible only"}) : null])]);
    }))]),
    h("div", {class: "stack-sm"}, [h("h4", {text: "Decision"}), h("dl", {class: "reasons"}, decision.flatMap(([k, val]) => [h("dt", {text: k}), h("dd", {text: val})]))]),
    fields.length ? h("div", {class: "stack-sm"}, [h("h4", {text: `The record (${fields.length} fields)`}),
      h("table", {class: "kv"}, fields.map(([k, val]) => h("tr", {}, [h("th", {text: k}),
        h("td", {class: [...values].some(x => String(val).toLowerCase().includes(x)) ? "hit" : "", text: val})])))]) : null,
    f.excerpt && !fields.length ? h("div", {class: "stack-sm"}, [h("h4", {text: "The text"}), h("pre", {class: "excerpt", text: f.excerpt})]) : null,
  ]);
}

// ---------------------------------------------------------------------------
// Timeline

function timelineTab(entries) {
  if (!entries.length) return emptyState("clock", "No dated items", "None of the matched records carries a date.");
  const box = h("div", {class: "timeline"});
  let day = "";
  for (const e of entries) {
    const d = e.time ? e.time.slice(0, 10) : "Undated";
    if (d !== day) { day = d; box.append(h("div", {class: "tl-day", text: d === "Undated" ? d : new Date(d + "T00:00:00Z").toLocaleDateString(undefined, {year: "numeric", month: "long", day: "numeric"})})); }
    const cls = e.type === "source captured" ? "captured" : e.type === "check time" ? "check" : e.type === "recorded event" ? "event" : "";
    box.append(h("div", {class: `tl-item ${cls}`}, [h("div", {class: "row between"}, [h("strong", {class: "small", text: e.type}),
      h("span", {class: "when", text: e.time ? e.time.slice(11, 19) : ""})]),
      h("div", {class: "small wrap", text: e.what}),
      e.captured_at ? h("div", {class: "tiny faint", text: `from a file captured ${when(e.captured_at)}`}) : null]));
  }
  return h("div", {class: "stack"}, [h("p", {class: "muted small", text: "Dated records, identity changes and file captures in order. Event time and capture time are shown separately; GhostTrace does not guess causes."}), box]);
}

// ---------------------------------------------------------------------------
// Cleanup plan: a checklist kept in this browser

function planTab(id, plan, byId, open) {
  if (!plan.length) return emptyState("checkCircle", "No steps", "Nothing about this person was found, so there is nothing to clean up.");
  const done = new Set(remember(`plan.${id}`) || []);
  const progressText = h("span", {class: "small muted"});
  const bar = h("div", {class: "progress", style: "width:200px"}, [h("i")]);
  const update = () => {
    progressText.textContent = `${done.size} of ${plan.length} steps ticked`;
    bar.firstChild.style.width = `${Math.round(done.size / plan.length * 100)}%`;
    remember(`plan.${id}`, [...done]);
  };
  const phases = PHASES.map(([phase, title, color]) => {
    const steps = plan.filter(s => s.phase === phase);
    if (!steps.length) return null;
    const dot = h("span", {class: "dot"});
    dot.style.background = color;
    // "What was found" repeats the summary, so it starts folded.
    return h("details", {class: "card", open: phase !== "FINDING"}, [h("summary", {class: "card-head"}, [h("div", {class: "phasehead"}, [icon("chevronRight", "sm chev"), dot, h("h2", {text: title})]),
      h("span", {class: "tiny faint", text: `${steps.length} steps`})]),
      h("div", {class: "planphase"}, steps.map(s => {
        const key = String(s.step_no);
        const box = h("input", {type: "checkbox", checked: done.has(key), "aria-label": `Step ${s.step_no} done`});
        const row = h("div", {class: `planstep ${done.has(key) ? "done" : ""}`}, [box, h("span", {class: "stepno", text: String(s.step_no)}),
          h("div", {class: "grow", style: "min-width:0"}, [h("div", {class: "what", text: OPERATION[s.operation] || s.operation}),
            h("div", {class: "where", text: `${s.where || s.system_id}${s.category ? ` · ${categoryLabel(s.category)}` : ""}`}),
            s.evidence_id ? h("div", {class: "tiny faint", text: `Proof: ${s.evidence_id}`}) : null]),
          byId[s.resource_id] ? h("button", {class: "btn sm ghost", type: "button", text: s.resource_id, onclick: () => open(s.resource_id)}) : null]);
        box.addEventListener("change", () => { if (box.checked) done.add(key); else done.delete(key); row.classList.toggle("done", box.checked); update(); });
        return row;
      }))]);
  });
  update();
  const asText = () => plan.map(s => `${s.step_no}. [${s.phase}] ${OPERATION[s.operation] || s.operation} — ${s.where || s.system_id}`).join("\n");
  return h("div", {class: "stack"}, [
    h("div", {class: "row between"}, [h("p", {class: "muted small", style: "max-width:720px", text: "Syncs are turned off and backups blocked before the records they feed are changed, so the cleanup cannot be undone. Tick steps as your team completes them; ticks are kept in this browser only."}),
      h("div", {class: "row"}, [bar, progressText,
        h("button", {class: "btn sm", type: "button", onclick: async () => {
          try { await navigator.clipboard.writeText(asText()); toast("Plan copied as text."); } catch (err) { download(`${id}-plan.txt`, asText()); }
        }}, [icon("copy", "sm"), "Copy"])])]),
    ...phases]);
}

// ---------------------------------------------------------------------------
// What-if

function whatifPanel(id, items) {
  let actions = [];
  const proposal = h("div", {class: "stack-sm"});
  const output = h("div", {class: "stack-sm"});
  const question = h("input", {type: "text", placeholder: "e.g. What if I delete the search entries and turn off the connector?", "aria-label": "Ask a what-if question"});
  const run = async text => {
    try {
      const response = await post("/api/whatif", {check_id: id, actions, question: text || ""});
      actions = response.actions;
      draw(response);
    } catch (err) { clear(output, errorNotice(err)); }
  };
  const draw = response => {
    clear(proposal, actions.length ? actions.map((a, i) => h("div", {class: "proposal"}, [icon("wand", "sm"),
      h("span", {class: "grow"}, [h("strong", {text: OPERATION[a.action] || a.action}), ` — ${a.target}`]),
      h("button", {class: "iconbtn", type: "button", "aria-label": "Remove", onclick: () => { actions.splice(i, 1); run(); }}, [icon("x", "sm")])])) :
      h("p", {class: "muted small", text: "No actions yet. Add some on the left, or ask a question."}));
    clear(output);
    if (!response) return;
    for (const m of response.messages || []) output.append(notice("warn", "info", `${m.status}: ${m.action} ${m.target || ""} — ${m.reason || m.question || ""}`));
    const interpretation = response.interpretation;
    if (interpretation) {
      output.append(h("div", {class: "tiny faint", text: `Question read by ${interpretation.interpreter === "local-rules" ? "the built-in rule reader (no AI)" : interpretation.interpreter}`}));
      for (const c of interpretation.clarify) output.append(notice("warn", "help", c.question));
      for (const rj of interpretation.rejected) output.append(notice("bad", "x", `Not possible: ${rj.action} ${rj.target || rj.resource_id || ""} — ${rj.reason}`));
    }
    const a = response.assessment;
    const outcome = a.would_be || a.hypothetical_status;
    const v = VERDICT[outcome] || VERDICT.NEEDS_REVIEW;
    output.append(h("section", {class: `verdict ${v.tone}`}, [h("div", {class: "vicon"}, [icon(v.icon)]),
      h("div", {}, [h("div", {class: "tiny", style: "font-weight:700;letter-spacing:.06em;text-transform:uppercase", text: "If you did this"}),
        h("h2", {text: v.title}), h("p", {class: "small", text: "Hypothetical only. Nothing was changed anywhere."})])]));
    const groups = a.outcome || {};
    for (const [key, tone, label] of [["policy conflict", "bad", "Conflicts with the policy"], ["still required", "bad", "Still to do"],
      ["needs review", "warn", "Needs review"], ["resolved", "good", "Resolved"]]) {
      const rows = groups[key] || [];
      if (!rows.length) continue;
      output.append(h("div", {class: "stack-sm"}, [h("h4", {text: `${label} (${rows.length})`}),
        h("ul", {class: "outcome", style: "margin:0;padding-left:18px"}, rows.map(e => h("li", {}, [badge(tone, e.id), " ", h("span", {class: "wrap", text: `${e.where} — ${e.reason}`})])))]));
    }
    for (const p of a.recreation_paths || []) {
      output.append(notice("bad", "repeat", `${p.process} still ${STATE_PHRASE[p.state] || p.state}${(p.would_recreate_changed || []).length ? ` and could bring back ${p.would_recreate_changed.join(", ")}` : ""}.`));
    }
    for (const w of a.order_warnings || []) output.append(notice("warn", "alert", w));
    output.append(h("details", {}, [h("summary", {class: "small muted", text: "Full explanation"}), h("pre", {class: "explain", text: response.explanation})]));
  };
  const rows = items.map(f => {
    const select = h("select", {"aria-label": `Action for ${f.id}`}, [h("option", {value: "", text: ACTION_FOR[f.required_state] ? "Choose…" : "Keep (the policy retains it)"}),
      ...ACTIONS.map(x => h("option", {value: x, text: OPERATION[x]}))]);
    select.value = ACTION_FOR[f.required_state] || "";
    return h("tr", {}, [h("td", {class: "mono faint", text: f.id}),
      h("td", {}, [h("div", {class: "small", style: "font-weight:600", text: categoryLabel(f.category)}), h("div", {class: "tiny muted wrap", text: `${f.source} / ${f.location}`})]),
      h("td", {}, [select]),
      h("td", {}, [h("button", {class: "btn sm", type: "button", onclick: () => {
        if (!select.value) return;
        actions = actions.filter(x => x.target !== f.id).concat([{action: select.value, target: f.id}]);
        run();
      }}, [icon("plus", "sm"), "Add"])])]);
  });
  const ask = () => { if (question.value.trim()) run(question.value); };
  question.addEventListener("keydown", ev => { if (ev.key === "Enter") ask(); });
  const examples = ["Apply the plan", "Delete the search entries", "Turn off the connector and delete the newsletter list"];
  draw(null);
  return h("div", {class: "stack"}, [
    notice("info", "sparkle", "Test cleanup ideas before touching any system. Proposals are tried on a temporary copy: nothing is deleted, the saved result does not change, and no system is contacted."),
    h("div", {class: "whatif-grid"}, [
      h("div", {class: "stack"}, [
        card("Ask in plain words", null, [h("div", {class: "row"}, [h("div", {class: "grow"}, [question]),
          h("button", {class: "btn primary", type: "button", onclick: ask}, [icon("sparkle", "sm"), "Ask"])]),
          h("div", {class: "row", style: "margin-top:8px"}, examples.map(text => h("button", {class: "chip", type: "button", style: "cursor:pointer", text,
            onclick: () => { question.value = text; ask(); }})))]),
        card("Or choose actions", null, items.length ? table(["", "Item", "Action", ""], rows) : h("p", {class: "muted", style: "padding:16px", text: "No items to act on."}), null, "flush")]),
      h("div", {class: "stack"}, [card("Your scenario", "Actions add up. Reset to start again.", [proposal, h("div", {style: "margin-top:10px"}, [
        h("button", {class: "btn sm", type: "button", onclick: () => { actions = []; run(); }}, [icon("refresh", "sm"), "Reset"])])]), output])])]);
}

// ---------------------------------------------------------------------------
// Identifiers and suggested links

function identifiersTab(saved) {
  const r = saved.result;
  const chosen = new Map();
  const rerun = h("button", {class: "btn primary", type: "button", disabled: true}, [icon("refresh", "sm"), "Run again with my answers"]);
  rerun.addEventListener("click", async () => {
    const req = saved.request;
    const confirmed = [...(req.confirmed || []), ...[...chosen].filter(([, value]) => value === "confirmed").map(([key]) => key)];
    const rejected = [...(req.rejected || []), ...[...chosen].filter(([, value]) => value === "rejected").map(([key]) => key)];
    try {
      const again = await post("/api/check", {...req, confirmed, rejected});
      location.hash = `#/result/${again.id}`;
    } catch (err) { toast(err.message, "bad"); }
  });
  const suggestions = r.suggested_identifiers.map(item => {
    const yes = h("button", {class: "btn sm", type: "button"}, [icon("check", "sm"), "Same person"]);
    const no = h("button", {class: "btn sm", type: "button"}, [icon("x", "sm"), "Not them"]);
    const pick = value => {
      chosen.set(item.key, value);
      yes.classList.toggle("primary", value === "confirmed");
      no.classList.toggle("danger", value === "rejected");
      rerun.disabled = false;
    };
    yes.addEventListener("click", () => pick("confirmed"));
    no.addEventListener("click", () => pick("rejected"));
    return h("div", {class: "findcard", style: "cursor:default"}, [icon((ID_KIND[item.kind] || ID_KIND.id).icon),
      h("div", {class: "grow"}, [h("div", {class: "where", text: item.value}), h("div", {class: "why", text: `${item.how} — found in ${item.evidence}`})]),
      h("div", {class: "row"}, [yes, no])]);
  });
  return h("div", {class: "stack"}, [
    suggestions.length ? card("Suggested links", "These values sit next to the person's data but could belong to someone else. Confirm only what you know.",
      [h("div", {}, suggestions), h("div", {style: "padding:12px 16px"}, [rerun])], null, "flush") : null,
    card("Identifiers used", "What GhostTrace searched for, and how it knows each one belongs to the person.",
      table(["Kind", "Value", "Origin", "How it is known", "Found in"], r.subject.map(i => [
        h("span", {class: "row"}, [icon((ID_KIND[i.kind] || ID_KIND.id).icon, "sm"), (ID_KIND[i.kind] || {}).text || i.kind]),
        h("span", {class: "wrap", style: "font-weight:600", text: i.value}),
        badge(i.origin === "provided" ? "info" : i.origin === "linked" ? "good" : "warn", ORIGIN[i.origin] || i.origin),
        h("span", {class: "small", text: i.how}), h("span", {class: "small muted wrap", text: i.evidence || "—"})])), null, "flush"),
  ]);
}

// ---------------------------------------------------------------------------
// Coverage and policy

function coverageTab(r) {
  const c = r.coverage;
  return h("div", {class: "stack"}, [
    notice("neutral", "info", r.label),
    c.unreadable.length ? notice("bad", "alertCircle", `Not readable, so not checked: ${c.unreadable.join(", ")}`) : null,
    c.after_as_of.length ? notice("neutral", "history", `Captured after the check time, so not used: ${c.after_as_of.join(", ")}`) : null,
    card(`Files checked (${c.sources.length})`, null, table(["File", "Type", {text: "Records", num: true}, "Captured", "Notes"], c.sources.map(s => [
      h("span", {class: "wrap", style: "font-weight:600", text: s.name}), s.kind, n(s.records), when(s.captured_at),
      h("span", {class: "small muted wrap", text: [...s.errors, ...s.notes].join("; ")})])), null, "flush"),
    card("Policy used", "The rule for each kind of data at the time of this check.", table(["Rule", "Kind of data", "Condition", "Action"], r.policy.map(p => [
      h("span", {class: "mono", text: p.rule_id}), categoryLabel(p.data_category), h("span", {class: "small", text: p.condition}),
      badge((ACTION[p.required_action] || {}).tone, (ACTION[p.required_action] || {}).text || p.required_action)])), null, "flush"),
  ]);
}

