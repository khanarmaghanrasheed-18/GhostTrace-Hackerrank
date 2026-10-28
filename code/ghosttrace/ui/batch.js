/* Batch check: one verdict per row of a request list, with progress and history. */

import {ago, api, card, clear, download, emptyState, errorNotice, h, icon, n, post, postFile, segmented, stat, table, toast, VERDICT,
        verdictBadge, waitForJob} from "./core.js";
import {onLeave} from "./state.js";

const TEMPLATE = "request_id,email,phone,name,user_id,as_of,request_text\n" +
  "DSR-1001,jane.doe@example.com,,,,,Please delete my account and all my data.\n" +
  "DSR-1002,,+44 7700 900123,,C-10442,2026-09-21T00:00:00Z,Delete everything you hold about me.\n";

// The plain verdict sentences; only a row's own reason (e.g. "not a deletion request: ...") is worth repeating.
const GENERIC = ["Personal data still exists", "No confirmed remaining data, but some items need a person to review",
                 "Deleted: every copy found has the treatment the policy requires", "No trace found in the readable sources"];

export async function batchPage(batchId) {
  if (batchId) return batchResult(batchId);
  const history = await api("/api/batches");
  const status = h("div");
  const input = h("input", {type: "file", accept: ".csv,text/csv", class: "hidden"});
  let alive = true;
  onLeave(() => { alive = false; });
  const start = async file => {
    if (!file) return;
    clear(status, h("div", {class: "jobcard"}, [h("div", {class: "row"}, [h("span", {class: "spinner"}), h("strong", {text: `Starting ${file.name}…`})])]));
    try {
      const {job} = await postFile(`/api/batch?name=${encodeURIComponent(file.name)}`, file);
      const final = await waitForJob(job, state => {
        if (!alive) return;
        const p = state.progress || {};
        const bar = h("div", {class: "progress"}, [h("i")]);
        bar.firstChild.style.width = `${p.total ? Math.round(p.done / p.total * 100) : 3}%`;
        clear(status, h("div", {class: "jobcard", role: "status"}, [h("div", {class: "row"}, [h("span", {class: "spinner"}),
          h("strong", {text: `Checking ${file.name}: ${n(p.done || 0)} of ${n(p.total || 0)} requests`})]), bar,
          h("div", {class: "meta", text: "Each person is checked against every file. You can leave this page; the batch keeps running."})]));
      });
      toast(`Batch finished: ${file.name}.`);
      if (alive) location.hash = `#/batch/${final.result.batch_id}`;
    } catch (err) { clear(status, errorNotice(err)); }
  };
  input.addEventListener("change", () => start(input.files[0]));
  const zone = h("div", {class: "dropzone", role: "button", tabindex: "0", onclick: () => input.click(),
                         onkeydown: ev => { if (ev.key === "Enter") input.click(); }}, [icon("upload", "xl"),
    h("strong", {text: "Drop a CSV of deletion requests here, or click to choose"}),
    h("span", {class: "small", text: "One row per person. Several values in one cell can be separated with “;”."}), input]);
  zone.addEventListener("dragover", ev => { ev.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", ev => { ev.preventDefault(); zone.classList.remove("over"); start(ev.dataTransfer.files[0]); });
  const columns = [["email, phone, name", "Who to check. Any mix; at least one per row."], ["user_id, customer_id, id", "IDs of the person."],
    ["request_id, case_id", "Your reference for the row."], ["as_of, audit_at", "Judge the state at this time (optional)."],
    ["request_text", "The person's message. Rows that do not ask for deletion are marked as such, and unclear ones are flagged for review."]];
  return h("div", {class: "stack"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Batch check"}),
      h("p", {text: "Check many people at once, for example a month of deletion requests. Every row gets its own verdict and full result."})]),
      h("button", {class: "btn", type: "button", onclick: () => download("ghosttrace_requests_template.csv", TEMPLATE, "text/csv")}, [icon("download", "sm"), "Download a template"])]),
    h("div", {class: "grid-2"}, [
      h("div", {class: "stack"}, [zone, status]),
      card("Columns GhostTrace understands", null, table(["Column", "Meaning"], columns.map(([c, m]) => [h("code", {text: c}), h("span", {class: "small", text: m})])), null, "flush")]),
    card("Earlier batches", null, history.length ? table(["File", {text: "Requests", num: true}, "Verdicts", "When"], history.map(b => h("tr", {class: "click",
      onclick: () => { location.hash = `#/batch/${b.id}`; }}, [h("td", {style: "font-weight:600", text: b.file}), h("td", {class: "num", text: n(b.rows)}),
      h("td", {}, [h("div", {class: "row"}, Object.entries(b.by_status).map(([s, count]) => h("span", {class: "row", style: "gap:4px"}, [verdictBadge(s), h("span", {class: "small", text: n(count)})])))]),
      h("td", {class: "small", text: ago(b.created_at)})]))) : emptyState("users", "No batches yet", "Results of each batch are kept here."), null, history.length ? "flush" : ""),
  ]);
}

async function batchResult(id) {
  const batch = await api(`/api/batches/${id}`);
  const results = batch.results;
  let filter = "all";
  const body = h("div");
  const counts = {};
  for (const r of results) counts[r.status] = (counts[r.status] || 0) + 1;
  const draw = () => {
    const shown = results.filter(r => filter === "all" || r.status === filter);
    clear(body, table(["Request", "Verdict", {text: "Still exists", num: true}, {text: "Review", num: true}, "Where it remains", ""], shown.map(r =>
      h("tr", {class: r.check_id ? "click" : "", onclick: r.check_id ? () => { location.hash = `#/result/${r.check_id}`; } : null}, [
        h("td", {style: "font-weight:600", text: r.request}), h("td", {}, [verdictBadge(r.status), GENERIC.includes(r.status_text) ? null :
          h("div", {class: "tiny muted", style: "margin-top:3px;max-width:260px", text: r.status_text})]),
        h("td", {class: "num", text: n(r.still_exists)}), h("td", {class: "num", text: n(r.needs_review)}),
        h("td", {class: "small muted wrap"}, [h("div", {class: "clamp", title: r.where || "", text: r.where || "—"})]), h("td", {}, [r.check_id ? icon("chevronRight", "sm") : null])]))));
  };
  draw();
  return h("div", {class: "stack"}, [
    h("a", {class: "crumb", href: "#/batch"}, [icon("arrowLeft", "sm"), "Batch check"]),
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: batch.file}), h("p", {text: `${n(batch.rows)} requests, checked ${ago(batch.created_at)}. Click a row to open that person's full result.`})]),
      h("div", {class: "row"}, [h("a", {class: "btn", href: `/api/batches/${id}/export.csv`}, [icon("download", "sm"), "Download CSV"]),
        h("button", {class: "btn ghost danger", type: "button", title: "Delete this batch", "aria-label": "Delete this batch", onclick: async () => {
          await post("/api/batches/delete", {id}); toast("Batch deleted."); location.hash = "#/batch"; }}, [icon("trash", "sm")])])]),
    h("div", {class: "stats"}, Object.keys(VERDICT).filter(k => counts[k]).map(k => stat(VERDICT[k].text, n(counts[k]), null, VERDICT[k].tone === "neutral" ? "" : VERDICT[k].tone, VERDICT[k].icon))),
    card(null, null, [h("div", {style: "padding:12px 16px;border-bottom:1px solid var(--line)"}, [
      segmented([["all", "All", results.length], ...Object.keys(VERDICT).filter(k => counts[k]).map(k => [k, VERDICT[k].text, counts[k]])], filter, key => { filter = key; draw(); })]),
      body], null, "flush")]);
}
