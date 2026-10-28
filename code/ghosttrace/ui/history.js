/* Results: every saved check, filterable. */

import {ago, api, card, clear, emptyState, h, icon, n, segmented, table, VERDICT, verdictBadge, when, who} from "./core.js";

export async function historyPage() {
  const checks = await api("/api/checks");
  let filter = "all";
  let query = "";
  let showBatch = false;
  const body = h("div");
  const search = h("input", {type: "search", placeholder: "Find a person, reference or file…", "aria-label": "Find a check"});
  const batchToggle = h("label", {class: "switch small"}, [h("input", {type: "checkbox"}), "Include rows from batch checks"]);
  batchToggle.querySelector("input").addEventListener("change", ev => { showBatch = ev.target.checked; draw(); });
  const counts = {};
  for (const c of checks) counts[c.status] = (counts[c.status] || 0) + 1;
  const draw = () => {
    const shown = checks.filter(c => (showBatch || !c.request.batch) && (filter === "all" || c.status === filter) &&
      (!query || `${who(c.request)} ${c.request.label || ""} ${c.request.batch || ""}`.toLowerCase().includes(query)));
    clear(body, shown.length ? table(["Verdict", "Who", {text: "Still exists", num: true}, {text: "Review", num: true}, "Reference", "When"], shown.map(c =>
      h("tr", {class: "click", onclick: () => { location.hash = `#/result/${c.id}`; }}, [
        h("td", {}, [verdictBadge(c.status)]), h("td", {class: "wrap", style: "font-weight:600", text: who(c.request)}),
        h("td", {class: "num", text: n(c.counts.still_exists)}), h("td", {class: "num", text: n(c.counts.needs_review)}),
        h("td", {class: "small muted", text: c.request.batch ? `Batch: ${c.request.batch}` : (c.request.label || "—")}),
        h("td", {class: "small nowrap", title: when(c.created_at), text: ago(c.created_at)})]))) :
      emptyState("history", checks.length ? "No checks match" : "No checks yet",
        checks.length ? "Try another filter." : "Run a check and its result is saved here, on this computer.",
        checks.length ? null : [h("a", {class: "btn primary", href: "#/check"}, [icon("search", "sm"), "Check a person"])]));
  };
  search.addEventListener("input", () => { query = search.value.trim().toLowerCase(); draw(); });
  draw();
  return h("div", {class: "stack"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Results"}), h("p", {text: "Every check you have run, newest first. Results are saved in the local workspace."})]),
      h("a", {class: "btn primary", href: "#/check"}, [icon("search", "sm"), "New check"])]),
    card(null, null, [h("div", {class: "row between", style: "padding:12px 16px;border-bottom:1px solid var(--line)"}, [
      segmented([["all", "All", checks.length], ...Object.keys(VERDICT).filter(k => counts[k]).map(k => [k, VERDICT[k].text, counts[k]])], filter,
        key => { filter = key; draw(); }),
      h("div", {class: "row"}, [batchToggle, h("div", {style: "width:min(280px,100%)"}, [search])])]), body], null, "flush")]);
}
