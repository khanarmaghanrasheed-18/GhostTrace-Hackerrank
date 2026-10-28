/* Shared helpers for the GhostTrace app: DOM building, the local API, icons,
 * plain-language labels, toasts, drawers and dialogs.
 * Every value from the files is inserted as text (textContent), never as HTML. */

// ---------------------------------------------------------------------------
// DOM

export function h(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value;   // the page policy refuses inline style attributes
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of [].concat(children ?? [])) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(node, child);
    else node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node, ...children) {
  node.textContent = "";
  append(node, children);
  return node;
}

// ---------------------------------------------------------------------------
// Icons: small stroke drawings. "M.." is a path, "c:x,y,r" a circle,
// "r:x,y,w,h,rx" a rectangle, "e:x,y,rx,ry" an ellipse.

const ICONS = {
  ghost: ["M12 3a7 7 0 0 0-7 7v10l2.4-1.8L9.7 20 12 18.2 14.3 20l2.3-1.8L19 20V10a7 7 0 0 0-7-7z", "c:9.5,10.5,1", "c:14.5,10.5,1"],
  home: ["M3 10.5 12 3l9 7.5", "M5 9.5V21h14V9.5", "M10 21v-6h4v6"],
  search: ["c:11,11,7", "m20 20-3.6-3.6"],
  folder: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
  folderPlus: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", "M12 10v6", "M9 13h6"],
  upload: ["M12 16V4", "m7 9 5-5 5 5", "M5 20h14"],
  file: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5"],
  fileText: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5", "M9 13h6", "M9 17h6"],
  database: ["e:12,5,8,3", "M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5", "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"],
  image: ["r:3,3,18,18,2", "c:9,9,2", "m21 15-5-5L5 21"],
  sheet: ["r:3,3,18,18,2", "M3 9h18", "M3 15h18", "M9 3v18"],
  braces: ["M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5a2 2 0 0 0 2 2h1", "M16 3h1a2 2 0 0 1 2 2v5a2 2 0 0 0 2 2 2 2 0 0 0-2 2v5a2 2 0 0 1-2 2h-1"],
  archive: ["r:3,3,18,5,1", "M5 8v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8", "M10 12h4"],
  mail: ["r:3,5,18,14,2", "m3 7 9 6 9-6"],
  shield: ["M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"],
  shieldCheck: ["M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z", "m9 12 2 2 4-4"],
  sliders: ["M4 21v-7", "M4 10V3", "M12 21v-9", "M12 8V3", "M20 21v-5", "M20 12V3", "M1 14h6", "M9 8h6", "M17 16h6"],
  gear: ["c:12,12,3", "M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"],
  play: ["M7 4v16l13-8z"],
  x: ["M18 6 6 18", "M6 6l12 12"],
  check: ["M20 6 9 17l-5-5"],
  checkCircle: ["c:12,12,9", "m8.5 12 2.5 2.5 4.5-5"],
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"],
  alertCircle: ["c:12,12,9", "M12 8v4", "M12 16h.01"],
  info: ["c:12,12,9", "M12 16v-4", "M12 8h.01"],
  help: ["c:12,12,9", "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3", "M12 17h.01"],
  clock: ["c:12,12,9", "M12 7v5l3 2"],
  graph: ["c:18,5,3", "c:6,12,3", "c:18,19,3", "m8.6 13.5 6.8 4", "m15.4 6.5-6.8 4"],
  download: ["M12 4v12", "m7 11 5 5 5-5", "M5 20h14"],
  trash: ["M3 6h18", "M8 6V4h8v2", "M6 6l1 14h10l1-14"],
  refresh: ["M21 12a9 9 0 1 1-3-6.7L21 8", "M21 3v5h-5"],
  sparkle: ["M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z", "M19 17l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"],
  chevronRight: ["m9 6 6 6-6 6"],
  chevronLeft: ["m15 6-6 6 6 6"],
  chevronDown: ["m6 9 6 6 6-6"],
  arrowLeft: ["M19 12H5", "m12 19-7-7 7-7"],
  arrowRight: ["M5 12h14", "m12 5 7 7-7 7"],
  moon: ["M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"],
  sun: ["c:12,12,4", "M12 2v2", "M12 20v2", "m4.9 4.9 1.4 1.4", "m17.7 17.7 1.4 1.4", "M2 12h2", "M20 12h2", "m6.3 17.7-1.4 1.4", "m19.1 4.9-1.4 1.4"],
  monitor: ["r:3,4,18,12,2", "M8 20h8", "M12 16v4"],
  external: ["M14 4h6v6", "M10 14 20 4", "M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"],
  plus: ["M12 5v14", "M5 12h14"],
  flask: ["M9 3h6", "M10 3v6L4 19a1 1 0 0 0 .9 1.5h14.2A1 1 0 0 0 20 19l-6-10V3", "M7 14h10"],
  layers: ["m12 3 9 5-9 5-9-5z", "m3 13 9 5 9-5"],
  users: ["c:9,8,4", "M2 21a7 7 0 0 1 14 0", "M16 4a4 4 0 0 1 0 8", "M22 21a7 7 0 0 0-4-6.3"],
  user: ["c:12,8,4", "M4 21a8 8 0 0 1 16 0"],
  key: ["c:8,15,4", "m10.8 12.2 9.2-9.2", "m17 6 3 3"],
  phone: ["M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"],
  at: ["c:12,12,4", "M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-3.9 7.9"],
  hash: ["M4 9h16", "M4 15h16", "M10 3 8 21", "M16 3l-2 18"],
  link: ["M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1", "M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"],
  eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z", "c:12,12,3"],
  printer: ["M6 9V3h12v6", "r:3,9,18,8,2", "M6 14h12v7H6z"],
  copy: ["r:9,9,12,12,2", "M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"],
  list: ["M8 6h13", "M8 12h13", "M8 18h13", "M3 6h.01", "M3 12h.01", "M3 18h.01"],
  history: ["M3 12a9 9 0 1 0 3-6.7L3 8", "M3 3v5h5", "M12 7v5l4 2"],
  repeat: ["m17 2 4 4-4 4", "M3 11V9a3 3 0 0 1 3-3h15", "m7 22-4-4 4-4", "M21 13v2a3 3 0 0 1-3 3H3"],
  stop: ["c:12,12,9", "M8 12h8"],
  lock: ["r:4,11,16,10,2", "M8 11V7a4 4 0 0 1 8 0v4"],
  menu: ["M4 6h16", "M4 12h16", "M4 18h16"],
  maximize: ["M8 3H5a2 2 0 0 0-2 2v3", "M21 8V5a2 2 0 0 0-2-2h-3", "M3 16v3a2 2 0 0 0 2 2h3", "M16 21h3a2 2 0 0 0 2-2v-3"],
  filter: ["M3 5h18l-7 8v6l-4 2v-8z"],
  wand: ["m15 4 5 5L9 20H4v-5z", "M13 6l5 5"],
};

export function icon(name, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon ${cls || ""}`);
  svg.setAttribute("aria-hidden", "true");
  for (const part of ICONS[name] || ICONS.file) {
    let node;
    if (part.startsWith("c:")) {
      const [cx, cy, r] = part.slice(2).split(",");
      node = document.createElementNS(ns, "circle"); node.setAttribute("cx", cx); node.setAttribute("cy", cy); node.setAttribute("r", r);
    } else if (part.startsWith("r:")) {
      const [x, y, w, hgt, rx] = part.slice(2).split(",");
      node = document.createElementNS(ns, "rect");
      Object.entries({x, y, width: w, height: hgt, rx}).forEach(([k, v]) => node.setAttribute(k, v));
    } else if (part.startsWith("e:")) {
      const [cx, cy, rx, ry] = part.slice(2).split(",");
      node = document.createElementNS(ns, "ellipse");
      Object.entries({cx, cy, rx, ry}).forEach(([k, v]) => node.setAttribute(k, v));
    } else {
      node = document.createElementNS(ns, "path"); node.setAttribute("d", part);
    }
    svg.append(node);
  }
  return svg;
}

// ---------------------------------------------------------------------------
// The local API

export async function api(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error("GhostTrace is not answering. Is the program still running in its terminal window?");
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
  if (!response.ok) throw new Error((data && data.error) || response.statusText || "request failed");
  return data;
}
export const post = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
export const postFile = (path, file) => api(path, {method: "POST", body: file});

export async function waitForJob(job, onProgress) {
  let state = job;
  while (state && state.status === "running") {
    await new Promise(resolve => setTimeout(resolve, 600));
    state = await api(`/api/jobs/${job.id}`);
    if (onProgress) onProgress(state);
  }
  if (state && state.status === "failed") throw new Error(state.error || "the job failed");
  return state;
}

// ---------------------------------------------------------------------------
// Plain-language labels

export const VERDICT = {
  STILL_EXISTS: {text: "Still exists", tone: "bad", icon: "alert",
    title: "This person's data still exists",
    says: "At least one copy breaks the deletion policy, or a sync or backup can bring the data back.",
    next: "Follow the cleanup plan, then run the check again on fresh exports."},
  NEEDS_REVIEW: {text: "Needs review", tone: "warn", icon: "help",
    title: "Nothing confirmed remains, but some items need a person to look",
    says: "There are possible matches (a name, a misread character, a shared phone), unknown states, a form's claim, or files that could not be read.",
    next: "Open the items marked for review and confirm or reject them."},
  DELETED: {text: "Deleted", tone: "good", icon: "checkCircle",
    title: "Deleted: every copy has the treatment the policy requires",
    says: "Records the policy requires to keep (such as invoices) are still there, as they should be.",
    next: "Keep the report as evidence of the check."},
  NO_TRACE: {text: "No trace found", tone: "good", icon: "shieldCheck",
    title: "No trace of this person in the files supplied",
    says: "Nothing matching these identifiers was found in any readable file.",
    next: "This covers only the files you added. Add exports from any other system that may hold the person's data."},
  INCOMPLETE: {text: "Incomplete", tone: "bad", icon: "alert", title: "Deletion is incomplete"},
  COMPLETE: {text: "Complete", tone: "good", icon: "checkCircle", title: "Deletion is complete"},
  NOT_APPLICABLE: {text: "Not a deletion request", tone: "neutral", icon: "info", title: "Not a deletion request"},
};

export const FINDING = {
  "violation": {text: "Still exists", tone: "bad"},
  "compliant": {text: "Compliant", tone: "good"},
  "possible match": {text: "Possible match", tone: "warn"},
  "unknown": {text: "Unknown state", tone: "warn"},
  "claim": {text: "Form's claim", tone: "warn"},
  "history": {text: "Earlier copy", tone: "neutral"},
};

export const CATEGORY = {
  USER_PROFILE: "Customer profile", SECONDARY_IDENTIFIER: "Identifier map", SEARCH_INDEX_ENTRY: "Search index entry",
  CACHE_ENTRY: "Cache entry", SUPPORT_MESSAGE: "Support conversation", SUPPORT_ATTACHMENT: "Support attachment",
  ANALYTICS_PROFILE: "Analytics", PRODUCT_TELEMETRY: "Product telemetry", USER_EXPORT: "Data export",
  MARKETING_PROFILE: "Marketing list", EMAIL_DELIVERY_LOG: "Email log", SECURITY_AUDIT_LOG: "Security log",
  LEGAL_HOLD_RECORD: "Legal hold", BACKUP_ARCHIVE: "Backup", ACCOUNT_TOMBSTONE: "Deleted-account reference",
  FINANCIAL_RECORD: "Invoice / financial record", PERSONAL_RECORD: "Other personal record", DATA_FLOW: "Sync / integration",
  FORM_LOOKUP: "Search screen",
};
export const categoryLabel = code => CATEGORY[code] || (code || "").toLowerCase().replace(/_/g, " ");

export const ACTION = {
  DELETE: {text: "Delete", tone: "bad"}, REDACT: {text: "Redact", tone: "warn"}, ANONYMIZE: {text: "Anonymize", tone: "warn"},
  RETAIN: {text: "Keep", tone: "good"}, DISABLE: {text: "Disable", tone: "bad"},
};
export const OPERATION = {
  REPORT_REMAINING_RECORD: "Data still stored", REPORT_EXPOSED_PII: "Identifying details exposed",
  REPORT_ACTIVE_DATA_FLOW: "Sync still running", REPORT_RESTORE_RISK: "Backup can restore it",
  DELETE_RECORD: "Delete the record", REDACT_RECORD: "Redact the identifying details", ANONYMIZE_RECORD: "Anonymize the record",
  DISABLE_DATA_FLOW: "Turn off the sync", BLOCK_RESTORE: "Block restoring from the backup",
  VERIFY_RECORD_ABSENT: "Check the record is gone", VERIFY_RECORD_PRESENT: "Check the record is still kept",
  VERIFY_RECORD_REDACTED: "Check the details are redacted", VERIFY_RECORD_ANONYMIZED: "Check it can no longer be linked",
  VERIFY_DATA_FLOW_DISABLED: "Check the sync is off", VERIFY_RESTORE_BLOCKED: "Check the backup cannot restore it",
};
export const ID_KIND = {email: {text: "Email", icon: "at"}, phone: {text: "Phone", icon: "phone"}, id: {text: "ID", icon: "hash"},
                        name: {text: "Name", icon: "user"}, hash: {text: "Hash", icon: "key"}};
export const ORIGIN = {provided: "You entered it", linked: "Proven link", suggested: "Suggested"};

export function badge(tone, text, withDot) {
  return h("span", {class: `badge ${tone || "neutral"}`}, [withDot ? h("span", {class: "dot"}) : null, text]);
}
export const verdictBadge = status => badge((VERDICT[status] || {}).tone, (VERDICT[status] || {}).text || status, true);
export const findingBadge = verdict => badge((FINDING[verdict] || {}).tone, (FINDING[verdict] || {}).text || verdict);

export function who(request) {
  return [...(request.emails || []), ...(request.phones || []), ...(request.ids || []), ...(request.names || [])].join(", ") || "—";
}

// ---------------------------------------------------------------------------
// Formatting

export const n = value => Number(value || 0).toLocaleString();
export function bytes(size) {
  if (!size) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(size) / Math.log(1024)));
  return `${(size / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}
export function when(iso) {
  if (!iso) return "—";
  const date = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
}
export function ago(iso) {
  const date = new Date(iso);
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (Number.isNaN(seconds)) return iso || "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  if (seconds < 86400 * 7) return `${Math.floor(seconds / 86400)} d ago`;
  return when(iso);
}
export function plural(count, word, many) { return `${n(count)} ${count === 1 ? word : (many || word + "s")}`; }

// ---------------------------------------------------------------------------
// Feedback: toasts, dialogs, drawers

let toastBox = null;
export function toast(message, tone, link) {
  if (!toastBox) { toastBox = h("div", {class: "toasts", role: "status", "aria-live": "polite"}); document.body.append(toastBox); }
  const item = h("div", {class: `toast ${tone || ""}`}, [icon(tone === "bad" ? "alertCircle" : "checkCircle"),
    h("div", {class: "grow"}, [message, link ? [" ", h("a", {href: link.href, text: link.text})] : null])]);
  toastBox.append(item);
  setTimeout(() => item.remove(), tone === "bad" ? 7000 : 4200);
}

let openLayer = null;
export function closeLayer() {
  if (openLayer) { openLayer.forEach(node => node.remove()); openLayer = null; document.removeEventListener("keydown", escClose); }
}
function escClose(ev) { if (ev.key === "Escape") closeLayer(); }
function showLayer(panel) {
  closeLayer();
  const scrim = h("div", {class: "scrim", onclick: closeLayer});
  openLayer = [scrim, panel];
  document.body.append(scrim, panel);
  document.addEventListener("keydown", escClose);
  const focusable = panel.querySelector("input, select, textarea, button:not(.iconbtn)") || panel.querySelector("button");
  if (focusable) setTimeout(() => focusable.focus(), 30);
}

export function drawer(title, subtitle, body, actions) {
  const panel = h("aside", {class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": title}, [
    h("div", {class: "drawer-head"}, [h("div", {class: "grow"}, [h("h2", {text: title}), subtitle ? h("div", {class: "muted small", style: "margin-top:4px"}, subtitle) : null]),
      actions || null, h("button", {class: "iconbtn", type: "button", "aria-label": "Close", onclick: closeLayer}, [icon("x")])]),
    h("div", {class: "drawer-body"}, body)]);
  showLayer(panel);
  return panel;
}

export function modal(title, body, footer, cls) {
  const panel = h("div", {class: `modal ${cls || ""}`, role: "dialog", "aria-modal": "true", "aria-label": title}, [
    title ? h("div", {class: "modal-head"}, [h("h2", {class: "grow", text: title}),
      h("button", {class: "iconbtn", type: "button", "aria-label": "Close", onclick: closeLayer}, [icon("x")])]) : null,
    h("div", {class: "modal-body"}, body),
    footer && footer.length ? h("div", {class: "modal-foot"}, footer) : null]);
  showLayer(panel);
  return panel;
}

export function confirmDialog(title, text, okText, danger) {
  return new Promise(resolve => {
    const done = value => { closeLayer(); resolve(value); };
    modal(title, [h("p", {text})], [
      h("button", {class: "btn", type: "button", text: "Cancel", onclick: () => done(false)}),
      h("button", {class: `btn ${danger ? "danger" : "primary"}`, type: "button", text: okText || "OK", onclick: () => done(true)})]);
  });
}

export function errorNotice(err) {
  return h("div", {class: "notice bad", role: "alert"}, [icon("alertCircle"), h("div", {}, [err.message || String(err)])]);
}

export function emptyState(iconName, title, text, actions) {
  return h("div", {class: "empty"}, [icon(iconName), h("strong", {text: title}), text ? h("p", {text}) : null,
    actions ? h("div", {class: "row", style: "justify-content:center;margin-top:6px"}, actions) : null]);
}

export function card(title, subtitle, body, actions, bodyClass) {
  return h("section", {class: "card"}, [
    title ? h("div", {class: "card-head"}, [h("div", {}, [h("h2", {text: title}), subtitle ? h("p", {text: subtitle}) : null]),
      actions ? h("div", {class: "row"}, actions) : null]) : null,
    h("div", {class: `card-body ${bodyClass || ""}`}, body)]);
}

export function stat(label, value, note, tone, iconName) {
  return h("div", {class: `card stat ${tone || ""}`}, [h("div", {class: "label"}, [iconName ? icon(iconName, "sm") : null, label]),
    h("div", {class: "value", text: String(value)}), note ? h("div", {class: "note", text: note}) : null]);
}

export function table(headers, rows, options) {
  const opts = options || {};
  return h("div", {class: "tablewrap"}, [h("table", {class: "table"}, [
    h("thead", {}, [h("tr", {}, headers.map(x => h("th", {class: typeof x === "object" && x.num ? "num" : null, text: typeof x === "object" ? x.text : x})))]),
    h("tbody", {}, rows.map((row, i) => row instanceof Node ? row : h("tr", {class: opts.onRow ? "click" : null, onclick: opts.onRow ? () => opts.onRow(i) : null},
      row.map((value, c) => {
        const num = typeof headers[c] === "object" && headers[c].num;
        return value instanceof Node || Array.isArray(value) ? h("td", {class: num ? "num" : null}, value)
          : h("td", {class: num ? "num" : null, text: value === null || value === undefined ? "" : String(value)});
      }))))])]);
}

export function segmented(options, value, onChange) {
  const box = h("div", {class: "segmented", role: "tablist"});
  const draw = current => {
    clear(box, options.map(([key, label, count]) => h("button", {type: "button", class: key === current ? "active" : "", role: "tab",
      "aria-selected": key === current ? "true" : "false", onclick: () => { draw(key); onChange(key); }},
      [label, count !== undefined ? h("span", {class: "n", text: n(count)}) : null])));
  };
  draw(value);
  return box;
}

export function tabs(items, initial, onChange) {
  const bar = h("div", {class: "tabs", role: "tablist"});
  const body = h("div", {role: "tabpanel"});
  const open = key => {
    for (const button of bar.children) button.classList.toggle("active", button.dataset.key === key);
    const item = items.find(i => i.key === key) || items[0];
    clear(body);
    try { body.append(item.render()); } catch (err) { body.append(errorNotice(err)); console.error(err); }
    if (onChange) onChange(item.key);
  };
  for (const item of items) {
    const button = h("button", {type: "button", role: "tab", onclick: () => open(item.key)},
      [item.icon ? icon(item.icon, "sm") : null, item.label, item.count !== undefined ? h("span", {class: "n", text: n(item.count)}) : null]);
    button.dataset.key = item.key;
    bar.append(button);
  }
  setTimeout(() => open(items.some(i => i.key === initial) ? initial : items[0].key));
  return h("div", {}, [bar, body]);
}

export function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], {type: type || "text/plain"}));
  const link = h("a", {href: url, download: name});
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function remember(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(`ghosttrace.${key}`) || "null");
    localStorage.setItem(`ghosttrace.${key}`, JSON.stringify(value));
  } catch (err) { return null; }
  return value;
}

export function kindIcon(kind, name) {
  const lower = (name || "").toLowerCase();
  if (kind === "database") return "database";
  if (kind === "image") return "image";
  if (kind === "spreadsheet") return "sheet";
  if (kind === "archive") return "archive";
  if (lower.endsWith(".eml") || lower.endsWith(".mbox")) return "mail";
  if (kind === "structured") return "braces";
  if (kind === "document") return "fileText";
  return "file";
}

/** Guess what kind of identifier a typed value is. */
export function guessKind(value) {
  const text = value.trim();
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text)) return "email";
  const digits = text.replace(/\D/g, "");
  if (/^\+?[\d\s().-]+$/.test(text) && digits.length >= 7) return "phone";
  if (/\d/.test(text) && !/\s/.test(text)) return "id";
  if (/^[0-9a-f]{32,64}$/i.test(text)) return "id";
  return "name";
}
