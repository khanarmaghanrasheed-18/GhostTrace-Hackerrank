/* Sources: the files GhostTrace reads, folders read in place, uploads and reading progress. */

import {api, bytes, card, categoryLabel, CATEGORY, clear, closeLayer, confirmDialog, drawer, emptyState, errorNotice, h, icon,
        kindIcon, modal, n, post, postFile, remember, segmented, table, tabs, toast, when} from "./core.js";
import {onLeave, onStatus, pollSoon, refreshStatus, state} from "./state.js";

// ---------------------------------------------------------------------------
// Adding data (also used by the home page and the shell)

export async function uploadFiles(files) {
  if (!files.length) return;
  let added = 0;
  for (const file of files) {
    try {
      await postFile(`/api/sources?name=${encodeURIComponent(file.name)}`, file);
      added++;
    } catch (err) { toast(`${file.name}: ${err.message}`, "bad"); }
  }
  if (added) toast(`${added === 1 ? files[0].name : `${added} files`} added. Reading ${added === 1 ? "it" : "them"} into the index…`);
  pollSoon();
}

export function pickFiles() {
  const input = h("input", {type: "file", multiple: true, class: "hidden"});
  input.addEventListener("change", () => { uploadFiles([...input.files]); input.remove(); });
  document.body.append(input);
  input.click();
}

export async function addDemo() {
  try {
    await post("/api/demo", {});
    toast("The fictional Acme Outdoor demo is being added. Try checking d.okafor@example.com.");
    pollSoon();
  } catch (err) { toast(err.message, "bad"); }
}

/** A folder browser for this computer. Reading happens in place: nothing is copied or changed. */
export function openFolderBrowser() {
  const pathInput = h("input", {type: "text", placeholder: "Type or paste a folder path, e.g. D:\\exports", "aria-label": "Folder path"});
  const listing = h("div", {class: "browser", "aria-label": "Folders"});
  const summary = h("div", {class: "small muted"});
  const roots = h("div", {class: "row"});
  const choose = h("button", {class: "btn primary", type: "button"}, [icon("folderPlus", "sm"), "Read this folder"]);
  let current = "";

  const go = async path => {
    clear(listing, h("div", {class: "row muted", style: "padding:12px"}, [h("span", {class: "spinner"}), "Opening…"]));
    try {
      const data = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
      current = data.path;
      pathInput.value = data.path;
      remember("lastFolder", data.path);
      clear(roots, [h("button", {class: "btn sm", type: "button", onclick: () => go(data.home)}, [icon("home", "sm"), "Home"]),
        ...data.roots.map(r => h("button", {class: "btn sm", type: "button", text: r, onclick: () => go(r)})),
        data.parent ? h("button", {class: "btn sm", type: "button", onclick: () => go(data.parent)}, [icon("arrowLeft", "sm"), "Up"]) : null]);
      const rows = data.entries.map(e => e.dir
        ? h("button", {type: "button", onclick: () => go(e.path)}, [icon("folder", "sm"), h("span", {class: "grow wrap", text: e.name}), icon("chevronRight", "sm")])
        : h("div", {class: `browser-file file ${e.supported ? "ok" : ""}`, style: "display:flex;gap:10px;padding:7px 12px;border-bottom:1px solid var(--line)"},
          [icon(e.supported ? "file" : "x", "sm"), h("span", {class: "grow wrap", text: e.name}), h("span", {class: "tiny", text: bytes(e.size)})]));
      clear(listing, rows.length ? rows : h("div", {class: "muted small", style: "padding:12px", text: "This folder is empty."}));
      summary.textContent = `${n(data.supported_files)} readable files directly in this folder` +
        (data.more ? `; ${n(data.more)} more entries not shown` : "") + ". Subfolders are read too.";
    } catch (err) { clear(listing, errorNotice(err)); }
  };
  pathInput.addEventListener("keydown", ev => { if (ev.key === "Enter") go(pathInput.value); });
  choose.addEventListener("click", async () => {
    const path = pathInput.value.trim() || current;
    if (!path) return;
    choose.disabled = true;
    try {
      await post("/api/folders", {path});
      closeLayer();
      toast("Folder added. GhostTrace reads it once into its index; you can keep working meanwhile.");
      pollSoon();
      if (location.hash !== "#/sources") location.hash = "#/sources";
      else document.dispatchEvent(new Event("ghosttrace:refresh-sources"));
    } catch (err) { toast(err.message, "bad"); choose.disabled = false; }
  });
  modal("Read a folder in place", [
    h("p", {class: "muted", text: "Pick the folder that holds the database files or exports. GhostTrace reads it where it is and never copies, changes or deletes anything in it."}),
    h("div", {class: "row"}, [h("div", {class: "grow"}, [pathInput]), h("button", {class: "btn", type: "button", text: "Open", onclick: () => go(pathInput.value)})]),
    roots, listing, summary], [h("button", {class: "btn", type: "button", text: "Cancel", onclick: closeLayer}), choose]);
  go(remember("lastFolder") || "");
}

// ---------------------------------------------------------------------------
// Reading progress

export function readingCard(reading) {
  const p = reading.progress || {};
  const total = p.files_total || 0;
  const done = p.files_done || 0;
  const bar = h("div", {class: `progress ${total ? "" : "indeterminate"}`}, [h("i")]);
  if (total) bar.firstChild.style.width = `${Math.max(3, Math.round(done / total * 100))}%`;
  const rate = p.reading && reading.seconds > 2 && p.records ? ` · about ${n(Math.round(p.records / reading.seconds))} records a second` : "";
  return h("div", {class: "jobcard", role: "status"}, [
    h("div", {class: "row"}, [h("span", {class: "spinner"}), h("strong", {text: total ? `Reading files: ${n(done)} of ${n(total)} ready` : "Reading files…"})]),
    bar,
    h("div", {class: "meta"}, p.file && p.reading ? `Now reading ${p.file}: ${n(p.records)} records so far${rate}.` :
      "Files read before are reused from the index; only new or changed files take time."),
  ]);
}

// ---------------------------------------------------------------------------
// The page

export async function sourcesPage() {
  const [list, folderData] = await Promise.all([api("/api/sources"), api("/api/folders"), refreshStatus()]);
  let sources = list;
  let folders = folderData.folders;
  let filter = remember("sourceFilter") || "all";
  let query = "";

  const progressBox = h("div");
  const folderBox = h("div");
  const filesBox = h("div");
  const search = h("input", {type: "search", placeholder: "Filter files…", "aria-label": "Filter files"});

  const reload = async () => {
    [sources, folders] = await Promise.all([api("/api/sources"), api("/api/folders").then(d => d.folders)]);
    drawFolders();
    drawFiles();
  };
  const listeners = [["ghosttrace:refresh-sources", reload], ["ghosttrace:add-folder", openFolderBrowser], ["ghosttrace:upload", pickFiles]];
  for (const [name, fn] of listeners) document.addEventListener(name, fn);
  onLeave(() => { for (const [name, fn] of listeners) document.removeEventListener(name, fn); });
  let wasReading = Boolean(state.status && state.status.reading);
  onStatus(status => {
    clear(progressBox, status.reading ? readingCard(status.reading) : null);
    if (status.reading || wasReading) reload();
    wasReading = Boolean(status.reading);
  });
  if (state.status && state.status.reading) progressBox.append(readingCard(state.status.reading));

  const drawFolders = () => {
    clear(folderBox, folders.length ? card("Folders read in place", "Large data stays where it is. GhostTrace reads each file once, and again only when it changes.",
      h("div", {class: "folderlist"}, folders.map(path => h("div", {class: "folderrow"}, [
        h("span", {class: "fileicon"}, [icon("folder")]),
        h("div", {class: "grow"}, [h("div", {class: "wrap", style: "font-weight:600", text: path}),
          h("div", {class: "tiny muted", text: `${n(sources.filter(s => s.linked && path.endsWith(s.name.split("/")[0])).length)} files`})]),
        h("button", {class: "btn sm", type: "button", onclick: async () => {
          if (!await confirmDialog("Stop reading this folder?", `GhostTrace will stop using ${path}. Your files there are not touched.`, "Stop reading")) return;
          try { await post("/api/folders/remove", {path}); toast("Folder removed from GhostTrace."); reload(); refreshStatus(); }
          catch (err) { toast(err.message, "bad"); }
        }}, [icon("x", "sm"), "Stop reading"])]))),
      [h("button", {class: "btn sm", type: "button", onclick: openFolderBrowser}, [icon("plus", "sm"), "Add folder"])]) : null);
  };

  const groups = {
    all: () => true, database: s => s.kind === "database", spreadsheet: s => s.kind === "spreadsheet",
    document: s => s.kind === "document" || s.kind === "structured", image: s => s.kind === "image",
    problems: s => !s.readable || s.errors.length > 0,
  };

  const drawFiles = () => {
    if (!sources.length) {
      clear(filesBox, card(null, null, emptyState("database", "No files yet",
        "Add the folders that hold database files or exports, or upload files. Spreadsheets, SQLite and SQL dumps, JSON, PDFs, Word, email, logs, screenshots and zip files all work.",
        [h("button", {class: "btn primary", type: "button", onclick: openFolderBrowser}, [icon("folderPlus", "sm"), "Add a folder"]),
          h("button", {class: "btn", type: "button", onclick: pickFiles}, [icon("upload", "sm"), "Upload files"]),
          state.status && state.status.demo_available ? h("button", {class: "btn ghost", type: "button", onclick: addDemo}, [icon("sparkle", "sm"), "Try the demo company"]) : null])));
      return;
    }
    const counts = Object.fromEntries(Object.entries(groups).map(([k, test]) => [k, sources.filter(test).length]));
    const shown = sources.filter(groups[filter] || groups.all).filter(s => !query || s.name.toLowerCase().includes(query));
    const rows = shown.map(s => {
      const settings = s.settings || {};
      const problem = !s.readable || s.errors.length;
      const status = s.status === "waiting" ? h("span", {class: "badge info"}, [h("span", {class: "spinner", style: "width:10px;height:10px"}), "Waiting"])
        : problem ? h("span", {class: "badge bad", title: s.errors.join("\n")}, [icon("alert", "sm"), s.readable ? "Partly read" : "Not readable"])
        : h("span", {class: "badge good", text: "Ready"});
      const row = h("tr", {class: "click", onclick: () => openSource(s, reload)}, [
        h("td", {}, [h("div", {class: "filecell"}, [h("span", {class: `fileicon ${s.kind}`}, [icon(kindIcon(s.kind, s.name))]),
          h("div", {}, [h("div", {class: "name", text: s.name}),
            h("div", {class: "tiny muted", text: [s.kind || "—", bytes(s.size), s.containers.length ? plural(s.containers.length, "table or sheet", "tables or sheets") : "",
              s.linked ? "in a linked folder" : ""].filter(Boolean).join(" · ")})])])]),
        h("td", {class: "num", text: n(s.records)}),
        h("td", {}, [settings.category ? h("span", {class: "badge info", text: categoryLabel(settings.category)}) : h("span", {class: "muted small", text: "Detected per record"})]),
        h("td", {class: "small", text: settings.captured_at ? when(settings.captured_at) : (s.captured_at ? `${when(s.captured_at)} (file date)` : "—")}),
        h("td", {}, [status]),
        h("td", {class: "right"}, [icon("chevronRight", "sm")])]);
      return row;
    });
    clear(filesBox, card(`Files (${n(sources.length)})`, `${n(sources.reduce((sum, s) => sum + s.records, 0))} records read. Click a file to preview what GhostTrace read and to adjust its settings.`,
      [h("div", {class: "row between", style: "padding:12px 16px;border-bottom:1px solid var(--line)"}, [
        segmented([["all", "All", counts.all], ["database", "Databases", counts.database], ["spreadsheet", "Spreadsheets", counts.spreadsheet],
          ["document", "Documents", counts.document], ["image", "Images", counts.image], ["problems", "Problems", counts.problems]],
          filter, key => { filter = key; remember("sourceFilter", key); drawFiles(); }),
        h("div", {style: "width:min(280px,100%)"}, [search])]),
       shown.length ? table(["File", {text: "Records", num: true}, "Kind of data", "Captured", "Status", ""], rows)
         : h("p", {class: "muted", style: "padding:16px", text: "No files match."})], null, "flush"));
  };
  search.addEventListener("input", () => { query = search.value.trim().toLowerCase(); drawFiles(); });

  drawFolders();
  drawFiles();
  return h("div", {class: "stack"}, [
    h("div", {class: "pagehead"}, [h("div", {}, [h("h1", {text: "Sources"}),
      h("p", {text: "The files GhostTrace checks. Everything stays on this computer: files you upload are copied into the local workspace, and linked folders are read where they are."})]),
      h("div", {class: "row"}, [
        h("button", {class: "btn primary", type: "button", onclick: openFolderBrowser}, [icon("folderPlus", "sm"), "Add folder"]),
        h("button", {class: "btn", type: "button", onclick: pickFiles}, [icon("upload", "sm"), "Upload files"]),
        h("button", {class: "btn ghost", type: "button", title: "More", onclick: () => moreMenu(reload)}, [icon("sliders", "sm"), "More"])])]),
    progressBox, folderBox, filesBox,
  ]);
}

function plural(count, word, many) { return `${n(count)} ${count === 1 ? word : many}`; }

function moreMenu(reload) {
  const manifest = h("input", {type: "file", accept: ".csv", class: "hidden"});
  manifest.addEventListener("change", async () => {
    try {
      const result = await postFile("/api/manifest", manifest.files[0]);
      closeLayer();
      toast(`Capture times updated for ${result.updated} files.`);
      reload();
    } catch (err) { toast(err.message, "bad"); }
  });
  modal("More options", [
    h("div", {class: "card stat"}, [h("h3", {text: "Import capture times"}),
      h("p", {class: "muted small", text: "A CSV with a file_path (or file/name) column and a captured_at column, and optionally category and store. Capture time decides which files count when you check “as of” a date."}),
      h("div", {}, [h("button", {class: "btn", type: "button", onclick: () => manifest.click()}, [icon("upload", "sm"), "Choose manifest CSV"]), manifest])]),
    h("div", {class: "card stat"}, [h("h3", {text: "Read every file again"}),
      h("p", {class: "muted small", text: "Files are read again automatically when they change. Use this if a reading tool (Tesseract, Poppler) was installed after a file was added."}),
      h("div", {}, [h("button", {class: "btn", type: "button", onclick: async () => {
        await post("/api/reread", {}); closeLayer(); toast("Reading the files again…"); pollSoon(); }}, [icon("refresh", "sm"), "Read again"])])]),
    state.status && state.status.demo_available ? h("div", {class: "card stat"}, [h("h3", {text: "Demo data"}),
      h("p", {class: "muted small", text: "Adds the fictional Acme Outdoor company: 12 files of every kind about one customer."}),
      h("div", {}, [h("button", {class: "btn", type: "button", onclick: () => { closeLayer(); addDemo(); }}, [icon("sparkle", "sm"), "Add the demo company"])])]) : null,
  ]);
}

// ---------------------------------------------------------------------------
// One file: preview and settings

function openSource(source, reload) {
  const settings = source.settings || {};
  const preview = h("div", {class: "stack-sm"});
  let offset = 0;
  const more = h("button", {class: "btn sm", type: "button", text: "Show the next 50"});
  const loadPreview = async () => {
    if (source.status === "waiting") {
      preview.append(h("div", {class: "notice info"}, [icon("info", "sm"), "This file is still being read. Its records appear here when the progress bar finishes."]));
      more.classList.add("hidden");
      return;
    }
    try {
      const data = await api(`/api/sources/preview?name=${encodeURIComponent(source.name)}&offset=${offset}`);
      for (const record of data.records) {
        const fields = Object.entries(record.fields);
        preview.append(h("div", {class: "card", style: "padding:10px 12px"}, [
          h("div", {class: "row between"}, [h("strong", {class: "small wrap", text: record.location}),
            h("span", {class: "tiny faint", text: [record.container, record.method, record.method.includes("ocr") ? `OCR ${Math.round(record.confidence * 100)}%` : ""].filter(Boolean).join(" · ")})]),
          fields.length ? h("table", {class: "kv", style: "margin-top:6px"}, fields.slice(0, 30).map(([k, v]) => h("tr", {}, [h("th", {text: k}), h("td", {text: v})]))) : null,
          record.text && !fields.length ? h("pre", {class: "excerpt", style: "margin-top:6px", text: record.text}) : null]));
      }
      offset += data.records.length;
      more.classList.toggle("hidden", offset >= data.count);
      if (!data.records.length && !offset) preview.append(h("p", {class: "muted", text: "No records were read from this file."}));
    } catch (err) { preview.append(errorNotice(err)); more.classList.add("hidden"); }
  };
  more.addEventListener("click", loadPreview);

  const category = h("select", {"aria-label": "Kind of data"}, [h("option", {value: "", text: "Detect for each record (recommended)"}),
    ...Object.keys(CATEGORY).filter(c => c !== "FORM_LOOKUP").map(c => h("option", {value: c, text: categoryLabel(c)}))]);
  category.value = settings.category || "";
  const captured = h("input", {type: "text", value: settings.captured_at || "", placeholder: source.captured_at ? `${source.captured_at} (the file's date)` : "e.g. 2026-09-15T12:00:00Z"});
  const store = h("input", {type: "text", value: settings.store || "", placeholder: "Same as the file name"});
  const complete = h("input", {type: "checkbox", checked: Boolean(settings.complete)});
  const save = h("button", {class: "btn primary", type: "button", onclick: async () => {
    try {
      await post("/api/sources/settings", {name: source.name, changes: {category: category.value, captured_at: captured.value, store: store.value, complete: complete.checked}});
      toast("Settings saved.");
      closeLayer();
      reload();
      pollSoon();
    } catch (err) { toast(err.message, "bad"); }
  }}, [icon("check", "sm"), "Save settings"]);
  const settingsForm = h("div", {class: "stack"}, [
    h("label", {class: "field"}, [h("span", {text: "Kind of data"}), category,
      h("span", {class: "hint", text: "Decides which policy rule applies. GhostTrace guesses it from table, sheet, file and column names; set it here when the guess is wrong for the whole file (for example, a sanctions screening list is a legal hold)."})]),
    h("label", {class: "field"}, [h("span", {text: "Captured at"}), captured,
      h("span", {class: "hint", text: "When this export or snapshot was taken. Used for checks “as of” a date. Without it, the file's modified date is used."})]),
    h("label", {class: "field"}, [h("span", {text: "Store"}), store,
      h("span", {class: "hint", text: "Give several files the same store name to treat them as copies of one system over time; the latest copy decides."})]),
    h("label", {class: "switch"}, [complete, h("span", {}, [h("strong", {text: "Complete snapshot "}),
      h("span", {class: "muted small", text: "— this file lists every record of its store, so a record missing from it proves it was removed."})])]),
    h("div", {}, [save])]);

  const notes = [...source.errors.map(e => h("div", {class: "notice bad"}, [icon("alertCircle", "sm"), e])),
                 ...source.notes.map(note => h("div", {class: "notice neutral"}, [icon("info", "sm"), note]))];
  const actions = source.linked ? null : h("button", {class: "btn sm danger", type: "button", onclick: async () => {
    if (!await confirmDialog("Remove this file?", `${source.name} will be deleted from the GhostTrace workspace. Saved results stay.`, "Remove", true)) return;
    try { await post("/api/sources/delete", {name: source.name}); toast("File removed."); reload(); refreshStatus(); }
    catch (err) { toast(err.message, "bad"); }
  }}, [icon("trash", "sm"), "Remove"]);
  drawer(source.name, `${source.kind || "file"} · ${bytes(source.size)} · ${n(source.records)} records${source.linked ? " · in a linked folder" : ""}`, [
    tabs([
      {key: "preview", label: "What was read", icon: "eye", render: () => { if (!offset) loadPreview(); return h("div", {class: "stack-sm"}, [
        h("p", {class: "muted small", text: "Each row, line or page GhostTrace found, with where it came from. This is what a check searches."}), preview, more]); }},
      {key: "settings", label: "Settings", icon: "sliders", render: () => settingsForm},
      {key: "notes", label: "Notes", icon: "info", count: notes.length, render: () => h("div", {class: "stack-sm"}, notes.length ? notes : [h("p", {class: "muted", text: "No notes."})])},
    ], source.errors.length ? "notes" : "preview")], actions);
}
