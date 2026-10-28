/* GhostTrace app shell: sidebar, top bar, router, command palette, file drop. */

import {clear, closeLayer, guessKind, h, icon, modal, n, post, remember, toast, VERDICT, who} from "./core.js";
import {leavePage, onStatusAlways, startPolling, state} from "./state.js";
import {homePage} from "./home.js";
import {sourcesPage, uploadFiles} from "./sources.js";
import {checkPage} from "./check.js";
import {resultPage} from "./result.js";
import {historyPage} from "./history.js";
import {batchPage} from "./batch.js";
import {policyPage} from "./policy.js";
import {settingsPage} from "./settings.js";

const NAV = [
  ["Overview", [["#/home", "Home", "home"]]],
  ["Investigate", [["#/check", "Check a person", "search"], ["#/results", "Results", "history"], ["#/batch", "Batch check", "users"]]],
  ["Data", [["#/sources", "Sources", "database"]]],
  ["Configure", [["#/policy", "Deletion policy", "shield"], ["#/settings", "Settings", "gear"]]],
];

const ROUTES = [
  [/^#\/home$/, homePage], [/^#\/sources$/, sourcesPage], [/^#\/check(?:\?(.*))?$/, checkPage],
  [/^#\/result\/([\w-]+)(?:\/(\w+))?$/, resultPage], [/^#\/results$/, historyPage],
  [/^#\/batch(?:\/([\w-]+))?$/, batchPage], [/^#\/policy$/, policyPage], [/^#\/settings$/, settingsPage],
];

// -- theme --------------------------------------------------------------------

export function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  remember("theme", theme || "system");
}
applyTheme(remember("theme"));

// -- shell --------------------------------------------------------------------

const root = document.getElementById("app");
const view = h("main", {class: "view", id: "view", tabindex: "-1"});
const navCounts = {};
const sidebar = h("nav", {class: "sidebar", "aria-label": "Main"}, [
  h("a", {class: "brand", href: "#/home"}, [h("span", {class: "logo"}, [icon("ghost")]),
    h("span", {}, ["GhostTrace", h("small", {text: "Is the data really deleted?"})])]),
  NAV.map(([group, links]) => h("div", {class: "navgroup"}, [h("span", {text: group}),
    links.map(([href, label, iconName]) => {
      const count = h("span", {class: "count"});
      navCounts[href] = count;
      return h("a", {class: "navlink", href}, [icon(iconName), label, count]);
    })])),
  h("div", {class: "sidebar-foot"}, [h("div", {id: "privacy"}),
    h("div", {class: "footlinks"}, [
      h("button", {class: "iconbtn", type: "button", title: "Switch light or dark", "aria-label": "Switch light or dark", onclick: cycleTheme}, [icon("moon")]),
      h("button", {class: "iconbtn", type: "button", title: "Search and commands (Ctrl+K)", "aria-label": "Search and commands", onclick: openPalette}, [icon("search")]),
    ])]),
]);
const jobchip = h("button", {class: "jobchip hidden", type: "button", onclick: () => { location.hash = "#/sources"; }});
const topbar = h("header", {class: "topbar"}, [
  h("button", {class: "iconbtn menu", type: "button", "aria-label": "Menu", onclick: () => shell.classList.toggle("nav-open")}, [icon("menu")]),
  h("button", {class: "searchtrigger", type: "button", onclick: openPalette}, [icon("search", "sm"), h("span", {}, ["Search", h("span", {class: "long", text: ", jump to a page or check someone"}), "…"]), h("kbd", {text: "Ctrl K"})]),
  jobchip]);
const shell = h("div", {class: "shell"}, [sidebar, h("div", {class: "main"}, [topbar, view])]);
root.append(shell);

function cycleTheme() {
  const order = ["system", "light", "dark"];
  const next = order[(order.indexOf(remember("theme") || "system") + 1) % order.length];
  applyTheme(next);
  toast(`Theme: ${next === "system" ? "same as this computer" : next}`);
}

function paintStatus(status) {
  clear(document.getElementById("privacy"), h("div", {class: "privacy good"}, [icon("lock", "sm"),
    h("span", {text: "Local only. No data leaves this computer."})]));
  navCounts["#/sources"].textContent = status.sources ? n(status.sources) : "";
  navCounts["#/results"].textContent = status.checks ? n(status.checks) : "";
  const reading = status.reading;
  jobchip.classList.toggle("hidden", !reading);
  if (reading) {
    const p = reading.progress || {};
    clear(jobchip, h("span", {class: "spinner"}),
      p.file && p.reading ? `Reading ${p.file.split("/").pop()} · ${n(p.records)} records` :
        `Reading files ${n(p.files_done || 0)} of ${n(p.files_total || 0)}`);
  }
}

// -- router ---------------------------------------------------------------------

let routeToken = 0;
async function route() {
  const hash = location.hash || "#/home";
  shell.classList.remove("nav-open");
  closeLayer();
  for (const link of sidebar.querySelectorAll(".navlink")) {
    const href = link.getAttribute("href");
    link.classList.toggle("active", hash === href || hash.startsWith(href + "/") || hash.startsWith(href + "?") ||
      (href === "#/results" && hash.startsWith("#/result/")));
  }
  const match = ROUTES.map(([pattern, page]) => [hash.match(pattern), page]).find(([found]) => found);
  const token = ++routeToken;
  leavePage();
  clear(view, h("div", {class: "stack"}, [h("div", {class: "skeleton", style: "height:28px;width:240px"}),
    h("div", {class: "skeleton", style: "height:120px"}), h("div", {class: "skeleton", style: "height:260px"})]));
  try {
    const page = match ? await match[1](...match[0].slice(1)) : await homePage();
    if (token !== routeToken) return;   // the user went elsewhere meanwhile
    clear(view, page);
  } catch (err) {
    if (token !== routeToken) return;
    clear(view, h("div", {class: "notice bad", role: "alert"}, [icon("alertCircle"),
      h("div", {}, [h("b", {text: "This page could not be shown. "}), err.message || String(err)])]));
    console.error(err);
  }
  view.focus({preventScroll: true});
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// -- command palette ---------------------------------------------------------

function openPalette() {
  const status = state.status || {recent_checks: []};
  const input = h("input", {type: "text", placeholder: "Type a page, an action, or an email / phone / ID to check…", "aria-label": "Search"});
  const list = h("ul", {role: "listbox"});
  let items = [];
  let active = 0;
  const base = [
    ...NAV.flatMap(([, links]) => links.map(([href, label, iconName]) => ({group: "Go to", label, icon: iconName, run: () => { location.hash = href; }}))),
    {group: "Actions", label: "Add a folder to read", icon: "folderPlus", run: () => { location.hash = "#/sources"; setTimeout(() => document.dispatchEvent(new Event("ghosttrace:add-folder")), 400); }},
    {group: "Actions", label: "Upload files", icon: "upload", run: () => { location.hash = "#/sources"; setTimeout(() => document.dispatchEvent(new Event("ghosttrace:upload")), 400); }},
    {group: "Actions", label: "Run a batch check from a CSV", icon: "users", run: () => { location.hash = "#/batch"; }},
    {group: "Actions", label: "Switch light or dark theme", icon: "moon", run: cycleTheme},
    ...(status.recent_checks || []).slice(0, 8).map(c => ({group: "Recent checks", label: who(c.request), icon: VERDICT[c.status] ? VERDICT[c.status].icon : "search",
      hint: (VERDICT[c.status] || {}).text, run: () => { location.hash = `#/result/${c.id}`; }})),
  ];
  const draw = () => {
    const query = input.value.trim().toLowerCase();
    items = base.filter(item => !query || item.label.toLowerCase().includes(query) || item.group.toLowerCase().includes(query));
    if (query.length > 2) {
      const raw = input.value.trim();
      items.unshift({group: "Check", label: `Check ${raw}`, icon: "play", hint: guessKind(raw), run: () => quickCheck(raw)});
    }
    active = Math.min(active, Math.max(items.length - 1, 0));
    clear(list);
    let group = "";
    items.forEach((item, i) => {
      if (item.group !== group) { group = item.group; list.append(h("li", {class: "group", text: group})); }
      const row = h("li", {class: i === active ? "active" : "", role: "option", onclick: () => { closeLayer(); item.run(); }},
        [icon(item.icon, "sm"), h("span", {class: "grow", text: item.label}), item.hint ? h("span", {class: "hint", text: item.hint}) : null]);
      row.addEventListener("mousemove", () => { if (active !== i) { active = i; draw(); } });
      list.append(row);
    });
    if (!items.length) list.append(h("li", {class: "group", text: "Nothing matches."}));
  };
  input.addEventListener("input", () => { active = 0; draw(); });
  input.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown") { ev.preventDefault(); active = Math.min(active + 1, items.length - 1); draw(); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); active = Math.max(active - 1, 0); draw(); }
    else if (ev.key === "Enter" && items[active]) { ev.preventDefault(); closeLayer(); items[active].run(); }
  });
  modal(null, [input, list], null, "palette");
  draw();
}

export async function quickCheck(text) {
  const body = {emails: [], phones: [], names: [], ids: []};
  for (const value of text.split(/[,;\n]+/).map(v => v.trim()).filter(Boolean)) body[`${guessKind(value)}s`].push(value);
  try {
    toast("Checking every file…");
    const result = await post("/api/check", body);
    location.hash = `#/result/${result.id}`;
  } catch (err) { toast(err.message, "bad"); }
}

document.addEventListener("keydown", ev => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); openPalette(); }
});

// -- dropping files anywhere ---------------------------------------------------

let dragDepth = 0;
const droplayer = h("div", {class: "droplayer hidden"}, [h("div", {class: "row"}, [icon("upload", "xl"), "Drop to add these files"])]);
document.body.append(droplayer);
const hasFiles = ev => [...(ev.dataTransfer ? ev.dataTransfer.types : [])].includes("Files");
window.addEventListener("dragenter", ev => { if (hasFiles(ev)) { dragDepth++; droplayer.classList.remove("hidden"); } });
window.addEventListener("dragleave", ev => { if (hasFiles(ev) && --dragDepth <= 0) { dragDepth = 0; droplayer.classList.add("hidden"); } });
window.addEventListener("dragover", ev => { if (hasFiles(ev)) ev.preventDefault(); });
window.addEventListener("drop", ev => {
  if (!hasFiles(ev)) return;
  ev.preventDefault();
  dragDepth = 0;
  droplayer.classList.add("hidden");
  if (ev.target.closest && ev.target.closest(".dropzone")) return;   // the page's own drop zone handles it
  if (location.hash.startsWith("#/batch")) return;
  uploadFiles([...ev.dataTransfer.files]).then(() => { if (location.hash !== "#/sources") location.hash = "#/sources"; });
});

// -- start --------------------------------------------------------------------

onStatusAlways(paintStatus);
startPolling();
route();
