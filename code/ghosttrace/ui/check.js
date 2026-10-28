/* Check a person: identifiers as chips, options, and what counts as proof. */

import {ago, card, clear, errorNotice, guessKind, h, icon, ID_KIND, n, post, verdictBadge, who} from "./core.js";
import {onStatus, refreshStatus} from "./state.js";
import {readingCard} from "./sources.js";

export async function checkPage(query) {
  const status = await refreshStatus();
  const params = new URLSearchParams(query || "");
  const chips = [];   // {kind, value}
  const box = h("div", {class: "bigsearch", onclick: () => input.focus()});
  const input = h("input", {type: "text", placeholder: "Type an email, phone, ID or name, then press Enter", "aria-label": "Identifier"});
  const asOf = h("input", {type: "datetime-local", "aria-label": "Check as of"});
  const label = h("input", {type: "text", placeholder: "e.g. ticket DSR-1042", "aria-label": "Reference"});
  const message = h("div");
  const run = h("button", {class: "btn primary lg", type: "button"}, [icon("play", "sm"), "Run check"]);
  const progress = h("div");

  const draw = () => {
    clear(box, chips.map((chip, i) => {
      const kind = h("select", {"aria-label": "Kind"}, Object.entries(ID_KIND).filter(([k]) => k !== "hash").map(([k, v]) => h("option", {value: k, text: v.text})));
      kind.value = chip.kind;
      kind.addEventListener("change", () => { chip.kind = kind.value; });
      kind.addEventListener("click", ev => ev.stopPropagation());
      return h("span", {class: "idchip"}, [icon(ID_KIND[chip.kind].icon, "sm"), kind, h("span", {class: "v", text: chip.value}),
        h("button", {type: "button", "aria-label": `Remove ${chip.value}`, onclick: ev => { ev.stopPropagation(); chips.splice(i, 1); draw(); input.focus(); }}, [icon("x", "sm")])]);
    }), input);
    input.placeholder = chips.length ? "Add another…" : "Type an email, phone, ID or name, then press Enter";
  };
  const add = text => {
    for (const value of text.split(/[\n,;]+/).map(v => v.trim()).filter(Boolean)) {
      if (!chips.some(c => c.value.toLowerCase() === value.toLowerCase())) chips.push({kind: guessKind(value), value});
    }
    input.value = "";
    draw();
  };
  input.addEventListener("keydown", ev => {
    if ((ev.key === "Enter" || ev.key === ",") && input.value.trim()) { ev.preventDefault(); add(input.value); }
    else if (ev.key === "Enter" && chips.length) { ev.preventDefault(); start(); }
    else if (ev.key === "Backspace" && !input.value && chips.length) { chips.pop(); draw(); }
  });
  input.addEventListener("paste", ev => {
    const text = ev.clipboardData.getData("text");
    if (/[\n,;]/.test(text)) { ev.preventDefault(); add(text); }
  });
  input.addEventListener("blur", () => { if (input.value.trim()) add(input.value); });

  const start = async () => {
    if (input.value.trim()) add(input.value);
    clear(message);
    if (!chips.length) { message.append(errorNotice(new Error("Add at least one email, phone, ID or name."))); input.focus(); return; }
    const body = {emails: [], phones: [], names: [], ids: [], label: label.value.trim(),
                  as_of: asOf.value ? new Date(asOf.value).toISOString().replace(/\.\d+Z$/, "Z") : ""};
    for (const chip of chips) body[`${chip.kind}s`].push(chip.value);
    run.disabled = true;
    clear(run, h("span", {class: "spinner"}), `Searching ${n(status.records)} records…`);
    try {
      const result = await post("/api/check", body);
      location.hash = `#/result/${result.id}`;
    } catch (err) {
      message.append(errorNotice(err));
      run.disabled = false;
      clear(run, icon("play", "sm"), "Run check");
    }
  };
  run.addEventListener("click", start);

  for (const key of ["email", "phone", "id", "name"]) for (const value of params.getAll(key)) chips.push({kind: key, value});
  draw();
  setTimeout(() => input.focus(), 50);

  const blocked = h("div");
  const paint = s => {
    clear(progress, s.reading ? readingCard(s.reading) : null);
    clear(blocked, !s.sources ? h("div", {class: "notice warn"}, [icon("info"), h("div", {}, ["There are no files yet. ",
      h("a", {href: "#/sources", text: "Add a folder or upload files"}), " first."])]) : null);
    run.disabled = !s.ready || !s.sources;
  };
  paint(status);
  onStatus(paint);

  const proof = (iconName, title, text) => h("div", {class: "proof"}, [icon(iconName), h("div", {}, [h("strong", {text: title}), text])]);
  return h("div", {class: "stack"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Check a person"}),
      h("p", {text: "Enter everything you know about the person. GhostTrace searches every file, follows the links it can prove (the ID on their profile, an old email in a change log) and suggests weaker ones for you to confirm."})])]),
    progress, blocked,
    card(null, null, [h("div", {class: "stack"}, [
      box,
      h("div", {class: "grid-2"}, [
        h("label", {class: "field"}, [h("span", {text: "Check as of (optional)"}), asOf,
          h("span", {class: "hint", text: "Judge the state at this moment: files captured later are left out."})]),
        h("label", {class: "field"}, [h("span", {text: "Reference (optional)"}), label,
          h("span", {class: "hint", text: "A ticket or request number, shown on the result and the report."})])]),
      h("div", {class: "row between"}, [h("span", {class: "small muted", text: "Tip: paste a list to add several at once. Click a chip's label to change its kind."}), run]),
      message])]),
    card("What counts as proof", "GhostTrace never treats similarity as proof.", [h("div", {class: "proofs"}, [
      proof("at", "Exact email", "In any letter case, anywhere in a record."),
      proof("phone", "Phone number", "In any format; compared on the last nine digits."),
      proof("hash", "ID or hash", "Customer numbers, user IDs, and SHA-256 / SHA-1 / MD5 of the email or phone."),
      proof("user", "Names: possible only", "Names are not unique, so they never prove a match on their own."),
      proof("image", "OCR near-misses: possible only", "A screenshot that reads “exampIe.com” is shown for review."),
      proof("users", "Shared phones: possible only", "A number on several people's profiles is never followed."),
    ])]),
    recentCard(status),
  ]);
}

function recentCard(status) {
  const recent = status.recent_checks.slice(0, 5);
  if (!recent.length) return null;
  return card("Recent checks", null, h("div", {}, recent.map(c => h("a", {class: "listrow", href: `#/result/${c.id}`}, [
    verdictBadge(c.status), h("span", {class: "who grow", text: who(c.request)}), h("span", {class: "tiny faint", text: ago(c.created_at)})]))),
    null, "flush");
}
