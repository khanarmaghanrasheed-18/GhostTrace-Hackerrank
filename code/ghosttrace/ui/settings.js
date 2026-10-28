/* Settings: privacy, appearance, the optional AI helper, the index. */

import {card, confirmDialog, h, icon, n, post, remember, segmented, toast} from "./core.js";
import {pollSoon, refreshStatus} from "./state.js";
import {applyTheme} from "./app.js";

export async function settingsPage() {
  const status = await refreshStatus();
  const ai = status.ai;
  const toggle = h("input", {type: "checkbox", checked: ai.allowed, disabled: !ai.key_configured && !ai.allowed});
  toggle.addEventListener("change", async () => {
    if (toggle.checked && !await confirmDialog("Turn on the AI helper?",
      "With the helper on, your what-if questions and the list of findings (kinds of data and file names) are sent to Google's Gemini API to turn questions into proposed actions. Checking files never uses it.", "Turn on")) {
      toggle.checked = false;
      return;
    }
    await post("/api/settings", {ai_allowed: toggle.checked});
    toast(toggle.checked ? "AI helper on for what-if questions." : "AI helper off. Nothing leaves this computer.");
    refreshStatus();
  });
  const copyPath = async () => {
    try { await navigator.clipboard.writeText(status.workspace); toast("Folder path copied."); } catch (err) { toast(status.workspace); }
  };
  const fact = (iconName, text) => h("div", {class: "row", style: "align-items:flex-start;flex-wrap:nowrap"}, [icon(iconName, "sm"), h("span", {class: "small", text})]);
  return h("div", {class: "stack", style: "max-width:860px"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Settings"}), h("p", {text: "GhostTrace runs only on this computer. These settings are kept in its workspace folder."})])]),
    card("Privacy", null, [h("div", {class: "stack-sm"}, [
      fact("lock", "The app answers only on this computer (127.0.0.1) and refuses requests from other sites."),
      fact("shield", "The page loads no outside scripts, fonts or images, and never sends your files anywhere."),
      fact("database", "Your files, the index of what was read, and every result are kept in this folder:"),
      h("div", {class: "row"}, [h("code", {class: "wrap", style: "padding:6px 10px;background:var(--surface-2);border-radius:8px", text: status.workspace}),
        h("button", {class: "btn sm", type: "button", onclick: copyPath}, [icon("copy", "sm"), "Copy"])]),
      fact("info", "To remove everything GhostTrace stored, close it and delete that folder. Linked folders are never changed.")])]),
    card("Appearance", null, [h("div", {class: "row between"}, [h("span", {class: "small", text: "Theme"}),
      segmented([["system", "Same as computer"], ["light", "Light"], ["dark", "Dark"]], remember("theme") || "system", key => applyTheme(key))])]),
    card("AI helper (optional)", "Off by default. Everything works without it.", [h("div", {class: "stack-sm"}, [
      h("label", {class: "switch"}, [toggle, h("span", {text: "Allow sending what-if questions to Gemini"})]),
      h("p", {class: "small muted", text: ai.key_configured
        ? "A GEMINI_API_KEY is set on this computer. When allowed, Gemini turns plain-English what-if questions into proposed actions; GhostTrace still checks every proposal."
        : "No GEMINI_API_KEY is set, so the helper cannot be turned on. A built-in rule reader answers what-if questions instead."}),
      h("p", {class: "tiny faint", text: `Calls this session: ${n(ai.usage.calls)} · tokens in/out: ${n(ai.usage.input_tokens)} / ${n(ai.usage.output_tokens)}`})])]),
    card("Index", "Every file is read once into a local index, and again only when it changes.", [h("div", {class: "row between"}, [
      h("span", {class: "small muted", text: `${n(status.sources)} files, ${n(status.records)} records.`}),
      h("button", {class: "btn", type: "button", onclick: async () => {
        await post("/api/reread", {}); toast("Reading the files again…"); pollSoon(); }}, [icon("refresh", "sm"), "Read all files again"])])]),
    card("Keyboard", null, [h("div", {class: "stack-sm small"}, [
      h("div", {class: "row"}, [h("kbd", {text: "Ctrl K"}), "Search, jump to a page, or check someone"]),
      h("div", {class: "row"}, [h("kbd", {text: "Esc"}), "Close a panel or dialog"])])]),
  ]);
}

