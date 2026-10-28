/* Settings: privacy, appearance, reading tools, the index, version. */

import {badge, card, h, icon, n, post, remember, segmented, toast} from "./core.js";
import {pollSoon, refreshStatus} from "./state.js";
import {applyTheme} from "./app.js";

export async function settingsPage() {
  const status = await refreshStatus();
  const copyPath = async () => {
    try { await navigator.clipboard.writeText(status.workspace); toast("Folder path copied."); } catch (err) { toast(status.workspace); }
  };
  const fact = (iconName, text) => h("div", {class: "row", style: "align-items:flex-start;flex-wrap:nowrap"}, [icon(iconName, "sm"), h("span", {class: "small", text})]);
  const missing = status.tools.filter(t => !t.found);
  return h("div", {class: "stack", style: "max-width:860px"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Settings"}), h("p", {text: "GhostTrace runs only on this computer and never connects to the internet."})])]),
    card("Privacy", null, [h("div", {class: "stack-sm"}, [
      fact("lock", "The app answers only on this computer (127.0.0.1) and refuses requests from other sites."),
      fact("shield", "It makes no outside connections: no AI service, no analytics, no updates. The page loads no outside scripts, fonts or images."),
      fact("database", "Your uploaded files, the index of what was read, and every result are kept in this folder:"),
      h("div", {class: "row"}, [h("code", {class: "wrap", style: "padding:6px 10px;background:var(--surface-2);border-radius:8px", text: status.workspace}),
        h("button", {class: "btn sm", type: "button", onclick: copyPath}, [icon("copy", "sm"), "Copy"])]),
      fact("info", "To remove everything GhostTrace stored, close it and delete that folder. Linked folders are never changed.")])]),
    card("Reading tools", missing.length ? "Some files cannot be read until these free tools are installed. Everything else works without them." :
      "Everything needed to read PDFs, scans and screenshots is installed.", [h("div", {class: "stack-sm"}, [
      ...status.tools.map(t => h("div", {class: "row between"}, [h("div", {}, [h("strong", {class: "small", text: t.name}),
        h("div", {class: "tiny muted", text: t.found ? t.path : `Needed for ${t.needed_for}`})]),
        badge(t.found ? "good" : "warn", t.found ? "Installed" : "Not installed")])),
      missing.length ? h("p", {class: "small muted", text: "Install them, then restart GhostTrace and use “Read all files again” below. The README lists the commands for Windows, macOS and Linux; in a terminal, `python code/main.py doctor` shows the same check."}) : null])]),
    card("Appearance", null, [h("div", {class: "row between"}, [h("span", {class: "small", text: "Theme"}),
      segmented([["system", "Same as computer"], ["light", "Light"], ["dark", "Dark"]], remember("theme") || "system", key => applyTheme(key))])]),
    card("Index", "Every file is read once into a local index, and again only when it changes.", [h("div", {class: "row between"}, [
      h("span", {class: "small muted", text: `${n(status.sources)} files, ${n(status.records)} records.`}),
      h("button", {class: "btn", type: "button", onclick: async () => {
        await post("/api/reread", {}); toast("Reading the files again…"); pollSoon(); }}, [icon("refresh", "sm"), "Read all files again"])])]),
    card("Keyboard", null, [h("div", {class: "stack-sm small"}, [
      h("div", {class: "row"}, [h("kbd", {text: "Ctrl K"}), "Search, jump to a page, or check someone"]),
      h("div", {class: "row"}, [h("kbd", {text: "Esc"}), "Close a panel or dialog"])])]),
    h("p", {class: "tiny faint", text: `GhostTrace ${status.version}`}),
  ]);
}
