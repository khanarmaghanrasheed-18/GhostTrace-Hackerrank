/* The deletion policy: one rule per kind of data, editable. */

import {ACTION, api, card, categoryLabel, clear, confirmDialog, download, h, icon, post, postFile, toast} from "./core.js";

const CHOICES = ["DELETE", "REDACT", "ANONYMIZE", "RETAIN", "DISABLE"];
const MEANING = {
  DELETE: "Remove the whole record.", REDACT: "Keep the record but remove what identifies the person.",
  ANONYMIZE: "Keep the record, irreversibly unlinked from the person.", RETAIN: "Must be kept (for example by law); keeping it is compliant.",
  DISABLE: "A sync or integration must be turned off.",
};

export async function policyPage() {
  const data = await api("/api/policy");
  const defaults = Object.fromEntries(data.default.map(r => [r.data_category, r]));
  let rules = data.rules.map(r => ({...r}));
  const save = h("button", {class: "btn primary", type: "button", disabled: true}, [icon("check", "sm"), "Save changes"]);
  const body = h("div");
  const banner = h("div");
  const dirty = () => rules.some((r, i) => r.required_action !== data.rules[i].required_action || r.condition !== data.rules[i].condition);
  const draw = () => {
    clear(banner, data.custom ? h("div", {class: "notice info"}, [icon("info"), h("div", {class: "grow"}, ["You are using a custom policy. Rules you did not change come from the default."]),
      h("button", {class: "btn sm", type: "button", onclick: reset}, [icon("refresh", "sm"), "Back to the default"])]) : null);
    clear(body, h("div", {class: "tablewrap"}, [h("table", {class: "table"}, [
      h("thead", {}, [h("tr", {}, ["Kind of data", "When it applies", "Required action", "Rule"].map(x => h("th", {text: x})))]),
      h("tbody", {}, rules.map(rule => {
        const base = defaults[rule.data_category] || {};
        const changed = rule.required_action !== base.required_action || rule.condition !== base.condition;
        const select = h("select", {class: `actionselect ${rule.required_action}`, "aria-label": `Action for ${categoryLabel(rule.data_category)}`},
          CHOICES.map(c => h("option", {value: c, text: ACTION[c].text})));
        select.value = rule.required_action;
        select.addEventListener("change", () => { rule.required_action = select.value; select.className = `actionselect ${select.value}`; save.disabled = !dirty(); draw(); });
        const condition = h("input", {type: "text", value: rule.condition, "aria-label": "Condition"});
        condition.addEventListener("change", () => { rule.condition = condition.value; save.disabled = !dirty(); });
        return h("tr", {class: changed ? "rule-changed" : ""}, [
          h("td", {}, [h("div", {style: "font-weight:600", text: categoryLabel(rule.data_category)}), h("div", {class: "tiny faint mono", text: rule.data_category})]),
          h("td", {style: "min-width:280px"}, [condition]),
          h("td", {}, [select, h("div", {class: "tiny muted", style: "margin-top:4px;max-width:260px", text: MEANING[rule.required_action] || ""})]),
          h("td", {class: "mono small"}, [rule.rule_id, changed ? h("div", {class: "tiny", style: "color:var(--accent)", text: "changed"}) : null])]);
      }))])]));
  };
  const reset = async () => {
    if (!await confirmDialog("Go back to the default policy?", "Your changed rules are removed. Saved results keep the policy they were checked with.", "Use the default")) return;
    await post("/api/policy/reset", {});
    toast("Back to the default policy.");
    location.reload();
  };
  save.addEventListener("click", async () => {
    try {
      const result = await post("/api/policy/rules", {rules});
      toast("Policy saved. New checks use it.");
      data.rules = result.rules.map(r => ({...r}));
      data.custom = result.custom;
      rules = result.rules.map(r => ({...r}));
      save.disabled = true;
      draw();
    } catch (err) { toast(err.message, "bad"); }
  });
  const importer = h("input", {type: "file", accept: ".csv", class: "hidden"});
  importer.addEventListener("change", async () => {
    try { await postFile("/api/policy", importer.files[0]); toast("Policy imported."); location.reload(); }
    catch (err) { toast(err.message, "bad"); }
  });
  const exportCsv = () => download("ghosttrace_policy.csv", ["rule_id,data_category,condition,required_action",
    ...rules.map(r => [r.rule_id, r.data_category, `"${String(r.condition).replace(/"/g, '""')}"`, r.required_action].join(","))].join("\n") + "\n", "text/csv");
  draw();
  return h("div", {class: "stack"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Deletion policy"}),
      h("p", {text: "What must happen to each kind of data when a person asks to be deleted. Every check applies these rules, and keeping what the law requires (invoices, security logs, legal holds) counts as compliant."})]),
      h("div", {class: "row"}, [h("button", {class: "btn", type: "button", onclick: () => importer.click()}, [icon("upload", "sm"), "Import CSV"]), importer,
        h("button", {class: "btn", type: "button", onclick: exportCsv}, [icon("download", "sm"), "Export CSV"]), save])]),
    banner,
    card(null, null, body, null, "flush"),
    h("p", {class: "small muted", text: "A CSV needs the columns rule_id, data_category, condition and required_action. Kinds of data it names replace the default rule; the rest stay."}),
  ]);
}
