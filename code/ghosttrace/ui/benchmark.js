/* The bundled Northstar Shop benchmark: 550 synthetic deletion requests. */

import {api, badge, card, clear, emptyState, errorNotice, h, icon, n, OPERATION, post, segmented, stat, table, tabs, VERDICT, verdictBadge} from "./core.js";
import {GraphView} from "./graph.js";
import {onLeave} from "./state.js";

export async function benchmarkPage() {
  const rows = await api("/api/benchmark");
  let filter = "all";
  let query = "";
  const body = h("div");
  const search = h("input", {type: "search", placeholder: "Case, email or person", "aria-label": "Find a case"});
  const draw = () => {
    const shown = rows.filter(r => (filter === "all" || (filter === "sample") === r.sample) &&
      (!query || `${r.case_id} ${r.email} ${r.person}`.toLowerCase().includes(query)));
    clear(body, shown.length ? table(["Case", "Email", "Person", "Workflow said", "GhostTrace verdict", {text: "Steps", num: true}],
      shown.slice(0, 600).map(r => h("tr", {class: "click", onclick: () => { location.hash = `#/benchmark/${r.case_id}`; }}, [
        h("td", {class: "mono", text: r.case_id}), h("td", {class: "wrap", text: r.email}), h("td", {class: "mono small", text: r.person || "—"}),
        h("td", {class: "small", text: r.reported_status}), h("td", {}, [verdictBadge(r.status)]), h("td", {class: "num", text: n(r.steps)})]))) :
      emptyState("filter", "No cases match", ""));
  };
  search.addEventListener("input", () => { query = search.value.toLowerCase(); draw(); });
  draw();
  const counts = {};
  for (const r of rows) counts[r.status] = (counts[r.status] || 0) + 1;
  return h("div", {class: "stack"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Benchmark: Northstar Shop"}),
      h("p", {text: "A synthetic deletion-audit dataset bundled for testing: 550 requests with screenshots, PDFs, SQL replicas, logs and JSON exports. The audit engine reconstructs each case at its audit time."})])]),
    h("div", {class: "stats"}, Object.entries(counts).map(([k, v]) => stat((VERDICT[k] || {}).text || k, n(v), null, (VERDICT[k] || {}).tone === "neutral" ? "" : (VERDICT[k] || {}).tone))),
    card(null, null, [h("div", {class: "row between", style: "padding:12px 16px;border-bottom:1px solid var(--line)"}, [
      segmented([["all", "All cases", rows.length], ["sample", "Public samples", rows.filter(r => r.sample).length], ["eval", "Evaluation", rows.filter(r => !r.sample).length]],
        filter, key => { filter = key; draw(); }), h("div", {style: "width:min(280px,100%)"}, [search])]), body], null, "flush")]);
}

export async function benchmarkCase(caseId) {
  const data = await api(`/api/benchmark/${caseId}`);
  const req = data.request;
  const v = VERDICT[data.status] || VERDICT.NOT_APPLICABLE;
  let graphView = null;
  onLeave(() => { if (graphView) graphView.destroy(); });
  return h("div", {class: "stack"}, [
    h("a", {class: "crumb", href: "#/benchmark"}, [icon("arrowLeft", "sm"), "Benchmark"]),
    h("div", {class: "pagehead", style: "margin-bottom:0"}, [h("div", {}, [h("h1", {text: `${caseId} · ${req.email}`}),
      h("p", {text: `Requested ${req.requested_at} · workflow said ${req.reported_status} at ${req.reported_at} · audited at ${req.audit_at}`})])]),
    h("section", {class: `verdict ${v.tone}`}, [h("div", {class: "vicon"}, [icon(v.icon)]), h("div", {}, [h("h2", {text: v.title}),
      h("p", {class: "small", text: `“${req.request_text}”`})])]),
    tabs([
      {key: "graph", label: "Graph", icon: "graph", render: () => { const box = h("div"); setTimeout(() => { graphView = new GraphView(box, data.graph); }); return box; }},
      {key: "steps", label: "Steps", icon: "list", count: data.steps.length, render: () => data.steps.length ? card(null, null, table(["#", "Phase", "Step", "System", "Resource", "Proof"],
        data.steps.map(s => [s.step_no, s.phase, OPERATION[s.operation] || s.operation, s.system_id, s.resource_id, s.evidence_id || "—"])), null, "flush")
        : emptyState("info", "No steps", "Not a deletion request.")},
      {key: "timeline", label: "Timeline", icon: "clock", render: () => card(null, null, table(["Time", "Type", "Item", "What"],
        data.timeline.entries.map(e => [e.time, e.type, e.source, e.what])), null, "flush")},
      {key: "whatif", label: "What-if", icon: "sparkle", render: () => benchWhatif(caseId, data.view.records)},
    ], "graph")]);
}

function benchWhatif(caseId, records) {
  const question = h("input", {type: "text", placeholder: "e.g. What if I disable the sync and delete the export?"});
  const out = h("div", {class: "stack-sm"});
  let actions = [];
  const run = async text => {
    try {
      const response = await post(`/api/benchmark/${caseId}/whatif`, {actions, question: text || ""});
      actions = response.actions;
      const verdict = response.assessment.hypothetical_status || response.assessment.would_be;
      clear(out, h("div", {class: "row"}, [h("strong", {text: "If you did this: "}), verdictBadge(verdict)]),
        h("div", {class: "row"}, actions.map(a => badge("info", `${a.action} ${a.target}`))),
        h("pre", {class: "explain", text: response.explanation}));
    } catch (err) { clear(out, errorNotice(err)); }
  };
  question.addEventListener("keydown", ev => { if (ev.key === "Enter") run(question.value); });
  return card("What-if on this case", `${n(records.length)} resources. Hypothetical only.`, [h("div", {class: "row"}, [h("div", {class: "grow"}, [question]),
    h("button", {class: "btn primary", type: "button", onclick: () => run(question.value)}, [icon("sparkle", "sm"), "Ask"]),
    h("button", {class: "btn", type: "button", onclick: () => { actions = []; run(); }}, [icon("refresh", "sm"), "Reset"])]), out]);
}
