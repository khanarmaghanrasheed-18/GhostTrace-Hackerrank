/* GhostTrace graph explorer: a dependency-free, force-directed SVG view.
 *
 * Data: {nodes: [{id, type, label, ...}], edges: [{source, target, relation, kind, evidence, time, note}]}
 * Drag nodes, drag the background to pan, scroll or use the buttons to zoom. Click a node to
 * see its connections (everything else fades), double-click to reveal its neighbours.
 * All text is inserted as text; file contents are never parsed as HTML.
 */

import {categoryLabel, clear, FINDING, h, icon} from "./core.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const TYPE_ORDER = ["person", "request", "identifier", "record", "process", "source", "system"];
const TYPE_LABEL = {person: "Person", request: "Request", identifier: "Identifiers", record: "Records",
                    process: "Syncs and backups", source: "Files", system: "Systems"};
const KIND_LABEL = {observed: "found in the data", provided: "entered by you", derived: "concluded by GhostTrace",
                    claimed: "reported by a system or form", suggested: "suggested, needs confirmation", weak: "possible match only"};
const RELATION = {PROVIDED: "entered by you", LINKED: "proven link", SUGGESTED: "suggested link", STORED_IN: "stored in",
                  APPEARS_IN: "appears in", POSSIBLE_MATCH: "possible match in", REVEALS: "reveals", SUPERSEDES: "later copy of",
                  CAN_RECREATE: "can bring back", SEARCHED_FOR: "searched for in"};

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

function niceLabel(node) {
  const label = node.label || node.id;
  const match = /^(F\d+) ([A-Z_]+)$/.exec(label);
  return match ? `${match[1]} · ${categoryLabel(match[2])}` : label;
}

function nodeClass(node) {
  const verdict = (node.verdict || "").replace(/\s+/g, "-");
  return `gnode type-${node.type}` + (verdict ? ` verdict-${verdict}` : "") + (node.origin ? ` origin-${node.origin}` : "");
}

export class GraphView {
  constructor(container, data, options) {
    this.options = options || {};
    this.nodes = data.nodes.map(node => Object.assign({}, node));
    this.byId = new Map(this.nodes.map(node => [node.id, node]));
    this.edges = data.edges.filter(e => this.byId.has(e.source) && this.byId.has(e.target));
    this.neighbours = new Map(this.nodes.map(node => [node.id, []]));
    for (const e of this.edges) {
      this.neighbours.get(e.source).push(e);
      this.neighbours.get(e.target).push(e);
    }
    this.hiddenTypes = new Set();
    this.visible = new Set(this.nodes.map(node => node.id));
    this.scale = 1; this.tx = 0; this.ty = 0;
    this.selected = null;
    this.build(container);
    this.layout();
    this.render();
    const start = this.nodes.find(node => node.type === "person") || this.nodes[0];
    if (start) this.select(start.id, true);
    requestAnimationFrame(() => this.fit());   // once the browser has sized the canvas
  }

  build(container) {
    clear(container);
    this.wrap = h("div", {class: "graphwrap"});
    const types = TYPE_ORDER.filter(t => this.nodes.some(node => node.type === t));
    this.search = h("input", {type: "search", placeholder: "Find an email, ID or file…", class: "gsearch", "aria-label": "Find a node"});
    this.search.addEventListener("input", () => this.highlight(this.search.value));
    this.search.addEventListener("keydown", ev => {
      if (ev.key !== "Enter") return;
      const hit = this.nodes.find(node => this.matches(node, this.search.value));
      if (hit) { this.reveal([hit.id]); this.select(hit.id); this.centerOn(hit); }
    });
    const filters = h("div", {class: "gfilters"}, types.map(t => {
      const count = this.nodes.filter(node => node.type === t).length;
      const chip = h("label", {class: "gfilter on"}, [h("input", {type: "checkbox", checked: true}), h("span", {class: `swatch type-${t}`}),
        `${TYPE_LABEL[t] || t} (${count})`]);
      chip.querySelector("input").addEventListener("change", ev => {
        chip.classList.toggle("on", ev.target.checked);
        if (ev.target.checked) this.hiddenTypes.delete(t); else this.hiddenTypes.add(t);
        this.render();
      });
      return chip;
    }));
    const tool = (iconName, label, action) => h("button", {class: "btn sm", type: "button", title: label, onclick: action}, [icon(iconName, "sm"), label]);
    const toolbar = h("div", {class: "gtoolbar"}, [this.search,
      tool("maximize", "Fit", () => this.fit()),
      tool("plus", "Zoom in", () => this.zoom(1.25)),
      tool("layers", "Show all", () => { this.visible = new Set(this.nodes.map(node => node.id)); this.render(); this.fit(); }),
      tool("refresh", "Re-arrange", () => { this.layout(true); this.render(); this.fit(); }),
      h("span", {class: "grow"}),
      tool("maximize", "Full screen", () => this.toggleFull())]);
    this.svg = svg("svg", {class: "gcanvas", role: "img", "aria-label": "Evidence graph"});
    const defs = svg("defs");
    const marker = svg("marker", {id: "arrow", viewBox: "0 0 10 10", refX: "21", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse"});
    marker.append(svg("path", {d: "M 0 0 L 10 5 L 0 10 z", class: "garrow"}));
    defs.append(marker);
    this.svg.append(defs);
    this.viewport = svg("g");
    this.edgeLayer = svg("g");
    this.nodeLayer = svg("g");
    this.viewport.append(this.edgeLayer, this.nodeLayer);
    this.svg.append(this.viewport);
    this.panel = h("aside", {class: "gpanel"}, [h("p", {class: "muted small", text: "Click a node to see how it is connected and where each connection comes from."})]);
    const legend = h("div", {class: "glegend"}, Object.entries(KIND_LABEL).map(([k, v]) =>
      h("span", {class: `lkind kind-${k}`}, [h("i"), v])));
    this.wrap.append(toolbar, filters, h("div", {class: "gstage"}, [this.svg, this.panel]), legend,
      h("p", {class: "faint tiny", text: "Drag nodes to move them, drag the background to pan, scroll to zoom, double-click a node to reveal its neighbours. Esc leaves full screen."}));
    container.append(this.wrap);
    this.bindPanZoom();
    this.escape = ev => { if (ev.key === "Escape" && this.wrap.classList.contains("full")) this.toggleFull(); };
    document.addEventListener("keydown", this.escape);
  }

  toggleFull() {
    this.wrap.classList.toggle("full");
    requestAnimationFrame(() => this.fit());
  }

  layout(reseed) {
    // Nodes repel, edges pull, and a weak pull keeps the graph centred.
    let seed = 7;
    const random = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    const count = this.nodes.length;
    this.nodes.forEach((node, i) => {
      if (reseed || node.x === undefined) {
        const ring = TYPE_ORDER.indexOf(node.type) + 1;
        const angle = (i / Math.max(count, 1)) * Math.PI * 2 + random();
        node.x = Math.cos(angle) * ring * 70 + random() * 20;
        node.y = Math.sin(angle) * ring * 70 + random() * 20;
      }
    });
    const k = 78;
    const steps = count > 400 ? 120 : 320;
    for (let step = 0; step < steps; step++) {
      const heat = 12 * (1 - step / steps) + 0.5;
      for (const node of this.nodes) { node.dx = -node.x * 0.025; node.dy = -node.y * 0.025; }
      for (let i = 0; i < count; i++) {
        for (let j = i + 1; j < count; j++) {
          const a = this.nodes[i], b = this.nodes[j];
          const dx = a.x - b.x, dy = a.y - b.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
          if (dist > 420) continue;
          const force = (k * k) / dist / dist;
          a.dx += dx * force; a.dy += dy * force; b.dx -= dx * force; b.dy -= dy * force;
        }
      }
      for (const e of this.edges) {
        const a = this.byId.get(e.source), b = this.byId.get(e.target);
        const dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const force = (dist - k) / dist * 0.08;
        a.dx -= dx * force; a.dy -= dy * force; b.dx += dx * force; b.dy += dy * force;
      }
      for (const node of this.nodes) {
        const length = Math.sqrt(node.dx * node.dx + node.dy * node.dy) || 1;
        node.x += node.dx / length * Math.min(length, heat);
        node.y += node.dy / length * Math.min(length, heat);
      }
    }
  }

  isShown(node) { return this.visible.has(node.id) && !this.hiddenTypes.has(node.type); }

  render() {
    clear(this.edgeLayer);
    clear(this.nodeLayer);
    this.lines = [];
    for (const e of this.edges) {
      const a = this.byId.get(e.source), b = this.byId.get(e.target);
      if (!this.isShown(a) || !this.isShown(b)) continue;
      const line = svg("line", {class: `gedge kind-${e.kind || "observed"}`, "marker-end": "url(#arrow)"});
      const title = svg("title");
      title.textContent = `${RELATION[e.relation] || e.relation} (${KIND_LABEL[e.kind] || e.kind})\n${e.evidence || ""}${e.time ? "\n" + e.time : ""}${e.note ? "\n" + e.note : ""}`;
      line.append(title);
      line.addEventListener("click", ev => { ev.stopPropagation(); this.showEdge(e); });
      this.edgeLayer.append(line);
      this.lines.push([line, a, b, e]);
    }
    this.groups = new Map();
    for (const node of this.nodes) {
      if (!this.isShown(node)) continue;
      const group = svg("g", {class: nodeClass(node), tabindex: "0", role: "button", "aria-label": niceLabel(node)});
      const big = node.type === "person";
      const shape = (node.type === "process" || node.type === "source" || node.type === "system")
        ? svg("rect", {x: "-11", y: "-11", width: "22", height: "22", rx: node.type === "source" ? "4" : "7"})
        : svg("circle", {r: big ? "17" : "11"});
      const text = niceLabel(node);
      const label = svg("text", {y: big ? "32" : "27", "text-anchor": "middle"});
      label.textContent = text.length > 32 ? text.slice(0, 30) + "…" : text;
      group.append(shape, label);
      this.bindNode(group, node);
      this.nodeLayer.append(group);
      this.groups.set(node.id, group);
    }
    this.place();
    this.highlight(this.search.value);
    this.dim();
  }

  place() {
    for (const [line, a, b] of this.lines) {
      line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
      line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    }
    for (const [id, group] of this.groups) {
      const node = this.byId.get(id);
      group.setAttribute("transform", `translate(${node.x},${node.y})`);
    }
    this.viewport.setAttribute("transform", `translate(${this.tx},${this.ty}) scale(${this.scale})`);
  }

  bindNode(group, node) {
    let dragging = false, moved = false, last = null;
    group.addEventListener("pointerdown", ev => {
      ev.stopPropagation(); dragging = true; moved = false; last = [ev.clientX, ev.clientY];
      group.setPointerCapture(ev.pointerId);
    });
    group.addEventListener("pointermove", ev => {
      if (!dragging) return;
      const dx = (ev.clientX - last[0]) / this.scale, dy = (ev.clientY - last[1]) / this.scale;
      if (Math.abs(dx) + Math.abs(dy) > 0.5) moved = true;
      node.x += dx; node.y += dy; last = [ev.clientX, ev.clientY];
      this.place();
    });
    group.addEventListener("pointerup", () => { dragging = false; if (!moved) this.select(node.id); });
    group.addEventListener("dblclick", ev => { ev.stopPropagation(); this.expand(node.id); });
    group.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); this.select(node.id); } });
  }

  bindPanZoom() {
    let panning = null;
    this.svg.addEventListener("pointerdown", ev => { panning = [ev.clientX, ev.clientY, this.tx, this.ty, false]; });
    this.svg.addEventListener("pointermove", ev => {
      if (!panning) return;
      if (Math.abs(ev.clientX - panning[0]) + Math.abs(ev.clientY - panning[1]) > 3) panning[4] = true;
      this.tx = panning[2] + ev.clientX - panning[0]; this.ty = panning[3] + ev.clientY - panning[1];
      this.place();
    });
    this.svg.addEventListener("pointerup", () => {
      if (panning && !panning[4] && this.selected) { this.selected = null; this.dim(); }   // a click on the background clears the focus
      panning = null;
    });
    window.addEventListener("pointerup", () => { panning = null; });
    this.svg.addEventListener("wheel", ev => {
      ev.preventDefault();
      const box = this.svg.getBoundingClientRect();
      this.zoom(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX - box.left, ev.clientY - box.top);
    }, {passive: false});
  }

  zoom(factor, mx, my) {
    const box = this.svg.getBoundingClientRect();
    const x = mx === undefined ? box.width / 2 : mx, y = my === undefined ? box.height / 2 : my;
    const scale = Math.min(4, Math.max(0.15, this.scale * factor));
    this.tx = x - (x - this.tx) * (scale / this.scale);
    this.ty = y - (y - this.ty) * (scale / this.scale);
    this.scale = scale;
    this.place();
  }

  fit() {
    const shown = this.nodes.filter(node => this.isShown(node));
    if (!shown.length) return;
    const box = this.svg.getBoundingClientRect();
    const width = box.width || 800, height = box.height || 600;
    const xs = shown.map(node => node.x), ys = shown.map(node => node.y);
    const minX = Math.min(...xs) - 70, maxX = Math.max(...xs) + 70, minY = Math.min(...ys) - 50, maxY = Math.max(...ys) + 60;
    const whole = Math.min(width / (maxX - minX), height / (maxY - minY));
    // Never shrink below a readable size; centre on the selected node instead.
    this.scale = Math.min(1.6, Math.max(0.55, whole));
    const anchor = whole < 0.55 && this.selected ? this.byId.get(this.selected) : null;
    const cx = anchor ? anchor.x : (minX + maxX) / 2, cy = anchor ? anchor.y : (minY + maxY) / 2;
    this.tx = width / 2 - cx * this.scale;
    this.ty = height / 2 - cy * this.scale;
    this.place();
  }

  centerOn(node) {
    const box = this.svg.getBoundingClientRect();
    this.tx = (box.width || 800) / 2 - node.x * this.scale;
    this.ty = (box.height || 600) / 2 - node.y * this.scale;
    this.place();
  }

  matches(node, text) {
    const query = (text || "").trim().toLowerCase();
    return Boolean(query) && (niceLabel(node).toLowerCase().includes(query) || node.id.toLowerCase().includes(query));
  }

  highlight(text) {
    for (const [id, group] of this.groups) group.classList.toggle("hit", this.matches(this.byId.get(id), text));
  }

  dim() {
    // With a node selected, everything not connected to it fades.
    const near = new Set();
    const on = Boolean(this.selected) && this.focused;
    if (on) {
      near.add(this.selected);
      for (const e of this.neighbours.get(this.selected) || []) { near.add(e.source); near.add(e.target); }
    }
    for (const [id, group] of this.groups) {
      group.classList.toggle("dim", on && !near.has(id));
      group.classList.toggle("selected", id === this.selected);
    }
    for (const [line, a, b] of this.lines) {
      line.classList.toggle("dim", on && a.id !== this.selected && b.id !== this.selected);
    }
  }

  reveal(ids) { for (const id of ids) this.visible.add(id); this.render(); }

  expand(id) {
    this.reveal([id, ...this.neighbours.get(id).map(e => (e.source === id ? e.target : e.source))]);
    this.select(id);
  }

  focus(hops) {
    if (!this.selected) return;
    const keep = new Set([this.selected]);
    let frontier = [this.selected];
    for (let step = 0; step < hops; step++) {
      const next = [];
      for (const id of frontier) {
        for (const e of this.neighbours.get(id)) {
          const other = e.source === id ? e.target : e.source;
          if (!keep.has(other)) { keep.add(other); next.push(other); }
        }
      }
      frontier = next;
    }
    this.visible = keep;
    this.render();
    this.fit();
  }

  select(id, quiet) {
    // The first selection only fills the side panel; fading starts when the user picks a node.
    this.selected = id;
    this.focused = !quiet;
    this.dim();
    const node = this.byId.get(id);
    if (!node) return;
    const details = [];
    const skip = ["x", "y", "dx", "dy", "id", "states", "label", "type"];
    for (const [key, value] of Object.entries(node)) {
      if (skip.includes(key) || value === "" || value === null || value === undefined) continue;
      const shown = key === "verdict" ? ((FINDING[value] || {}).text || value) : (typeof value === "object" ? JSON.stringify(value) : String(value));
      details.push(h("tr", {}, [h("th", {text: key.replace(/_/g, " ")}), h("td", {text: shown})]));
    }
    if (node.states) {
      for (const [store, info] of Object.entries(node.states)) {
        details.push(h("tr", {}, [h("th", {text: `state in ${store}`}),
          h("td", {text: `${info.state}${info.inferred ? " (inferred)" : ""} → ${info.verdict} [${info.evidence_id} ${info.observed_at || ""}]`})]));
      }
    }
    const links = this.neighbours.get(id).map(e => {
      const other = this.byId.get(e.source === id ? e.target : e.source);
      const outgoing = e.source === id;
      return h("li", {class: `kind-${e.kind}`}, [
        h("div", {class: "tiny faint", text: `${outgoing ? "" : "← "}${RELATION[e.relation] || e.relation}${outgoing ? " →" : ""} · ${KIND_LABEL[e.kind] || e.kind}`}),
        h("button", {type: "button", class: "linkbtn", text: niceLabel(other),
          onclick: () => { this.reveal([other.id]); this.select(other.id); this.centerOn(other); }}),
        e.evidence || e.note ? h("div", {class: "tiny muted wrap", text: [e.evidence, e.note].filter(Boolean).join(" — ")}) : null,
        e.time ? h("div", {class: "tiny faint", text: e.time}) : null]);
    });
    clear(this.panel,
      h("div", {class: "tiny faint", text: (TYPE_LABEL[node.type] || node.type).replace(/s$/, "")}),
      h("h3", {class: "wrap", text: niceLabel(node)}),
      node.finding && this.options.onFinding ? h("button", {class: "btn sm primary", type: "button", onclick: () => this.options.onFinding(node.finding)},
        [icon("eye", "sm"), "Open the evidence"]) : null,
      details.length ? h("table", {class: "kv"}, details) : null,
      h("h4", {text: `Connections (${links.length})`}),
      h("ul", {class: "glinks"}, links),
      h("div", {class: "row"}, [
        h("button", {class: "btn sm", type: "button", text: "Reveal neighbours", onclick: () => this.expand(id)}),
        h("button", {class: "btn sm", type: "button", text: "Only what is near", onclick: () => this.focus(1)}),
        h("button", {class: "btn sm", type: "button", text: "2 steps", onclick: () => this.focus(2)})]));
    if (!quiet && this.options.onSelect) this.options.onSelect(node);
  }

  showEdge(e) {
    const a = this.byId.get(e.source), b = this.byId.get(e.target);
    clear(this.panel, h("div", {class: "tiny faint", text: "Connection"}),
      h("h3", {text: RELATION[e.relation] || e.relation}),
      h("span", {class: "badge neutral", text: KIND_LABEL[e.kind] || e.kind}),
      h("table", {class: "kv"}, [["from", niceLabel(a)], ["to", niceLabel(b)], ["evidence", e.evidence], ["time", e.time], ["note", e.note]]
        .filter(row => row[1]).map(([k, v]) => h("tr", {}, [h("th", {text: k}), h("td", {text: v})]))));
  }

  destroy() { document.removeEventListener("keydown", this.escape); }
}
