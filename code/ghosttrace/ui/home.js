/* Home: a guided start when there is no data yet, otherwise an overview. */

import {ago, api, card, categoryLabel, clear, emptyState, h, icon, n, stat, verdictBadge, who} from "./core.js";
import {onStatus, refreshStatus} from "./state.js";
import {addDemo, openFolderBrowser, pickFiles, readingCard} from "./sources.js";
import {quickCheck} from "./app.js";

export async function homePage() {
  const status = await refreshStatus();
  const page = h("div");
  const progress = h("div");
  let shape = "";
  const draw = s => {
    const now = `${Boolean(s.sources || s.folders || s.reading)}|${s.sources}|${s.ready}|${s.checks}`;
    if (now !== shape) {   // redraw only when something on the page changes, not on every progress tick
      shape = now;
      clear(page, s.sources || s.folders || s.reading ? overview(s, progress) : welcome(s, progress));
    }
    clear(progress, s.reading ? readingCard(s.reading) : null);
  };
  draw(status);
  onStatus(draw);
  return page;
}

function welcome(status, progress) {
  const step = (number, title, text, actions) => h("div", {class: "step"}, [h("span", {class: "num", text: String(number)}),
    h("h3", {text: title}), h("p", {text}), actions ? h("div", {class: "row", style: "margin-top:auto;padding-top:6px"}, actions) : null]);
  return h("div", {class: "stack"}, [progress,
    h("section", {class: "hero"}, [
      h("div", {class: "row"}, [h("span", {class: "badge good"}, [icon("lock", "sm"), "Runs only on this computer"])]),
      h("h1", {style: "margin-top:14px", text: "Find out whether a person's data is really deleted"}),
      h("p", {text: "Give GhostTrace your database files, exports, spreadsheets, screenshots and documents. Then name a person by email, phone, name or ID. It shows every place their data still exists, why it counts, what can bring it back, and the cleanup steps that finish the job."}),
      h("div", {class: "steps"}, [
        step(1, "Add your data", "Point GhostTrace at the folders with your exports and database files. Big folders are read where they are, never copied.", [
          h("button", {class: "btn primary", type: "button", onclick: openFolderBrowser}, [icon("folderPlus", "sm"), "Choose a folder"]),
          h("button", {class: "btn", type: "button", onclick: pickFiles}, [icon("upload", "sm"), "Upload files"])]),
        step(2, "Check a person", "Enter their email, phone, name or ID, or upload a list of deletion requests. GhostTrace follows the links it can prove, such as an old email.", [
          status.demo_available ? h("button", {class: "btn", type: "button", onclick: addDemo}, [icon("sparkle", "sm"), "Try the demo company"]) : null]),
        step(3, "Act on the verdict", "Get the exact file, table and row where data remains, an ordered cleanup plan, and a report to attach to the ticket.", null),
      ]),
    ]),
    h("div", {class: "grid-3"}, [
      card("What it reads", null, [h("p", {class: "muted small", text: "SQLite databases and SQL dumps, CSV and Excel, JSON and XML, PDFs (including scans), Word and PowerPoint, emails and mailbox exports, contacts and calendars, logs, screenshots, and zip archives."})]),
      card("What counts as proof", null, [h("p", {class: "muted small", text: "An exact email, a phone number in any format, an ID, or a hash of the email or phone. A name alone is only a possible match, because names are not unique."})]),
      card("Where your data goes", null, [h("p", {class: "muted small", text: "Nowhere. The app answers only on this computer, loads nothing from the internet, and keeps files and results in its local workspace folder."})]),
    ]),
  ]);
}

function overview(status, progress) {
  const box = h("div", {class: "stack"});
  const input = h("input", {type: "text", placeholder: "Email, phone, name or ID — separate several with commas", "aria-label": "Who to check"});
  const run = h("button", {class: "btn primary lg", type: "button", disabled: !status.ready}, [icon("play", "sm"), "Check"]);
  const go = () => { if (input.value.trim()) quickCheck(input.value); };
  run.addEventListener("click", go);
  input.addEventListener("keydown", ev => { if (ev.key === "Enter") go(); });
  const by = status.checks_by_status;
  const recent = status.recent_checks;
  const attention = h("div");
  const leftovers = h("div");
  box.append(
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Home"}),
      h("p", {text: `${n(status.sources)} files and ${n(status.records)} records are ready to check.`})]),
      h("a", {class: "btn", href: "#/check"}, [icon("sliders", "sm"), "More options"])]),
    progress,
    card("Check a person", status.ready ? "Type what you know; GhostTrace works out which is an email, phone, ID or name." :
      "Files are still being read. You can check as soon as the progress bar finishes.",
      [h("div", {class: "quickcheck"}, [input, run])]),
    h("div", {class: "stats"}, [
      stat("Files", n(status.sources), `${n(status.records)} records`, "", "database"),
      stat("Checks run", n(status.checks), status.batches ? `${n(status.batches)} batches` : "", "", "history"),
      stat("Data still exists", n(by.STILL_EXISTS || 0), "checks with data left", by.STILL_EXISTS ? "bad" : "", "alert"),
      stat("Needs review", n(by.NEEDS_REVIEW || 0), "checks to look at", by.NEEDS_REVIEW ? "warn" : "", "help"),
      stat("Deleted or no trace", n((by.DELETED || 0) + (by.NO_TRACE || 0)), "clean checks", "good", "checkCircle"),
    ]),
    h("div", {class: "grid-2"}, [
      card("Recent checks", null, recent.length ? h("div", {}, recent.map(c => h("a", {class: "listrow", href: `#/result/${c.id}`}, [
        verdictBadge(c.status), h("span", {class: "who grow", text: who(c.request)}),
        c.counts.still_exists ? h("span", {class: "small bad", style: "color:var(--bad)", text: `${n(c.counts.still_exists)} left`}) : null,
        h("span", {class: "tiny faint nowrap", text: ago(c.created_at)})]))) :
        emptyState("search", "No checks yet", "Type an email, phone or ID above to run the first one."),
        [h("a", {class: "btn sm ghost", href: "#/results", text: "All results"})], recent.length ? "flush" : ""),
      h("div", {class: "stack"}, [leftovers, attention]),
    ]),
  );
  api("/api/checks").then(checks => {
    const counts = {};
    for (const c of checks) for (const cat of c.categories) counts[cat] = (counts[cat] || 0) + 1;
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 8);
    const max = Math.max(1, ...entries.map(e => e[1]));
    clear(leftovers, card("Where data is left behind", "Kinds of data still found, across all checks", entries.length ?
      h("div", {class: "bars"}, entries.map(([cat, count]) => {
        const fill = h("i");
        fill.style.width = `${Math.round(count / max * 100)}%`;
        return h("div", {class: "bar-row"}, [h("span", {class: "wrap", text: categoryLabel(cat)}), h("div", {class: "bar bad"}, [fill]), h("span", {class: "right small", text: n(count)})]);
      })) : h("p", {class: "muted small", text: "Nothing yet. Leftover data shows up here once checks find it."})));
  }).catch(() => {});
  api("/api/sources").then(sources => {
    const bad = sources.filter(s => !s.readable || s.errors.length);
    clear(attention, bad.length ? card("Files that need attention", "Data in these files could not be fully read, so a clean verdict is not given while they remain.",
      h("div", {}, bad.slice(0, 6).map(s => h("a", {class: "listrow", href: "#/sources"}, [icon("alert", "sm"),
        h("span", {class: "grow wrap"}, [h("div", {style: "font-weight:600", text: s.name}), h("div", {class: "tiny muted", text: s.errors[0] || ""})])]))), null, "flush") : null);
  }).catch(() => {});
  return box;
}

