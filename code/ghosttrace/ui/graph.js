/* GhostTrace graph explorer: a dependency-free, force-directed SVG view.
 *
 * Data: {nodes: [{id, type, label, ...}], edges: [{source, target, relation, kind, evidence, time, note}]}
 * Interaction: drag nodes, drag the background to pan, scroll to zoom, click a
 * node for its details and connections, double-click to reveal its neighbours,
 * "Focus" to show only what is near the selected node.
 * All text is inserted with textContent; file contents are never parsed as HTML.
 */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const TYPE_ORDER = ["person", "request", "identifier", "record", "process", "source", "system"];
const TYPE_LABEL = {person: "Person", request: "Request", identifier: "Identifier", record: "Record",
                    process: "Sync / restore process", source: "Source / evidence", system: "System"};
const KIND_LABEL = {observed: "observed in the data", provided: "entered by the user", derived: "concluded by GhostTrace",
                    claimed: "reported by a system", suggested: "suggested, needs confirmation", weak: "possible match only"};

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

function html(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = v;
    else if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children || []) if (child) node.append(child);
  return node;
}

function nodeClass(node) {
  const verdict = (node.verdict || "").replace(/\s+/g, "-");
  return `gnode type-${node.type}` + (verdict ? ` verdict-${verdict}` : "") + (node.origin ? ` origin-${node.origin}` : "");
}

class GraphView {
  constructor(container, data, options) {
    this.options = options || {};
    this.nodes = data.nodes.map(n => Object.assign({}, n));
    this.byId = new Map(this.nodes.map(n => [n.id, n]));
    this.edges = data.edges.filter(e => this.byId.has(e.source) && this.byId.has(e.target));
    this.neighbours = new Map(this.nodes.map(n => [n.id, []]));
    for (const e of this.edges) {
      this.neighbours.get(e.source).push(e);
      this.neighbours.get(e.target).push(e);
    }
    this.hiddenTypes = new Set();
    this.visible = new Set(this.nodes.map(n => n.id));
    this.scale = 1; this.tx = 0; this.ty = 0;
    this.selected = null;
    this.build(container);
    this.layout();
    this.render();
    const start = this.nodes.find(n => n.type === "person") || this.nodes[0];
    if (start) this.select(start.id);
    // Fit once the browser has given the canvas its final size.
    requestAnimationFrame(() => this.fit());
  }

  build(container) {
    container.textContent = "";
    const types = TYPE_ORDER.filter(t => this.nodes.some(n => n.type === t));
    this.search = html("input", {type: "search", placeholder: "Find a node (email, ID, file)…", class: "gsearch"});
    this.search.addEventListener("input", () => this.highlight(this.search.value));
    this.search.addEventListener("keydown", ev => {
      if (ev.key === "Enter") {
        const hit = this.nodes.find(n => this.matches(n, this.search.value));
        if (hit) { this.reveal([hit.id]); this.select(hit.id); this.centerOn(hit); }
      }
    });
    const filters = html("div", {class: "gfilters"}, types.map(t => {
      const box = html("input", {type: "checkbox", checked: "checked"});
      box.addEventListener("change", () => {
        if (box.checked) this.hiddenTypes.delete(t); else this.hiddenTypes.add(t);
        this.render();
      });
      return html("label", {class: `gfilter type-${t}`}, [box, html("span", {class: "swatch"}), html("span", {text: TYPE_LABEL[t] || t})]);
    }));
    const buttons = html("div", {class: "gbuttons"}, [
      html("button", {type: "button", text: "Fit", onclick: () => this.fit()}),
      html("button", {type: "button", text: "Show all", onclick: () => { this.visible = new Set(this.nodes.map(n => n.id)); this.render(); }}),
      html("button", {type: "button", text: "Focus on selected", onclick: () => this.focus(1)}),
      html("button", {type: "button", text: "Re-arrange", onclick: () => { this.layout(true); this.render(); this.fit(); }}),
    ]);
    const legend = html("div", {class: "glegend"}, Object.entries(KIND_LABEL).map(([k, v]) =>
      html("span", {class: `lkind kind-${k}`}, [html("i"), html("span", {text: `${k}: ${v}`})])));
    this.svg = svg("svg", {class: "gcanvas", role: "img", "aria-label": "Evidence graph"});
    const defs = svg("defs");
    const marker = svg("marker", {id: "arrow", viewBox: "0 0 10 10", refX: "22", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse"});
    marker.append(svg("path", {d: "M 0 0 L 10 5 L 0 10 z", class: "garrow"}));
    defs.append(marker);
    this.svg.append(defs);
    this.viewport = svg("g");
    this.edgeLayer = svg("g");
    this.nodeLayer = svg("g");
    this.viewport.append(this.edgeLayer, this.nodeLayer);
    this.svg.append(this.viewport);
    this.panel = html("aside", {class: "gpanel"}, [html("p", {class: "muted", text: "Click a node to see how it is connected and where each connection comes from."})]);
    const stage = html("div", {class: "gstage"}, [this.svg, this.panel]);
    container.append(html("div", {class: "gtoolbar"}, [this.search, buttons]), filters, stage, legend,
      html("p", {class: "muted small", text: "Drag nodes to move them, drag the background to pan, scroll to zoom, double-click a node to reveal its neighbours."}));
    this.bindPanZoom();
  }

  layout(reseed) {
    // A simple force layout: nodes repel, edges pull, a weak pull keeps the graph centred.
    let seed = 7;
    const random = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    const count = this.nodes.length;
    this.nodes.forEach((n, i) => {
      if (reseed || n.x === undefined) {
        const ring = TYPE_ORDER.indexOf(n.type) + 1;
        const angle = (i / Math.max(count, 1)) * Math.PI * 2 + random();
        n.x = Math.cos(angle) * ring * 60 + random() * 20;
        n.y = Math.sin(angle) * ring * 60 + random() * 20;
      }
    });
    const k = 70;
    for (let step = 0; step < 320; step++) {
      const heat = 12 * (1 - step / 320) + 0.5;
      for (const n of this.nodes) { n.dx = -n.x * 0.03; n.dy = -n.y * 0.03; }
      for (let i = 0; i < count; i++) {
        for (let j = i + 1; j < count; j++) {
          const a = this.nodes[i], b = this.nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
          if (dist > 400) continue;
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
      for (const n of this.nodes) {
        const length = Math.sqrt(n.dx * n.dx + n.dy * n.dy) || 1;
        n.x += n.dx / length * Math.min(length, heat);
        n.y += n.dy / length * Math.min(length, heat);
      }
    }
  }

  isShown(node) { return this.visible.has(node.id) && !this.hiddenTypes.has(node.type); }

  render() {
    this.edgeLayer.textContent = "";
    this.nodeLayer.textContent = "";
    this.lines = [];
    for (const e of this.edges) {
      const a = this.byId.get(e.source), b = this.byId.get(e.target);
      if (!this.isShown(a) || !this.isShown(b)) continue;
      const line = svg("line", {class: `gedge kind-${e.kind || "observed"}`, "marker-end": "url(#arrow)"});
      const title = svg("title");
      title.textContent = `${e.relation} (${e.kind})\n${e.evidence || ""}${e.time ? "\n" + e.time : ""}${e.note ? "\n" + e.note : ""}`;
      line.append(title);
      line.addEventListener("click", ev => { ev.stopPropagation(); this.showEdge(e); });
      this.edgeLayer.append(line);
      this.lines.push([line, a, b]);
    }
    this.groups = new Map();
    for (const n of this.nodes) {
      if (!this.isShown(n)) continue;
      const group = svg("g", {class: nodeClass(n) + (this.selected === n.id ? " selected" : ""), tabindex: "0"});
      const shape = (n.type === "process" || n.type === "source" || n.type === "system")
        ? svg("rect", {x: "-11", y: "-11", width: "22", height: "22", rx: n.type === "source" ? "3" : "6"})
        : svg("circle", {r: n.type === "person" ? "15" : "11"});
      const label = svg("text", {y: "26", "text-anchor": "middle"});
      label.textContent = (n.label || n.id).length > 34 ? (n.label || n.id).slice(0, 32) + "…" : (n.label || n.id);
      const title = svg("title");
      title.textContent = `${TYPE_LABEL[n.type] || n.type}: ${n.label || n.id}`;
      group.append(shape, label, title);
      this.bindNode(group, n);
      this.nodeLayer.append(group);
      this.groups.set(n.id, group);
    }
    this.place();
    this.highlight(this.search.value);
  }

  place() {
    for (const [line, a, b] of this.lines) {
      line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
      line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    }
    for (const [id, group] of this.groups) {
      const n = this.byId.get(id);
      group.setAttribute("transform", `translate(${n.x},${n.y})`);
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
    group.addEventListener("keydown", ev => { if (ev.key === "Enter") this.select(node.id); });
  }

  bindPanZoom() {
    let panning = null;
    this.svg.addEventListener("pointerdown", ev => { panning = [ev.clientX, ev.clientY, this.tx, this.ty]; });
    this.svg.addEventListener("pointermove", ev => {
      if (!panning) return;
      this.tx = panning[2] + ev.clientX - panning[0]; this.ty = panning[3] + ev.clientY - panning[1];
      this.place();
    });
    window.addEventListener("pointerup", () => { panning = null; });
    this.svg.addEventListener("wheel", ev => {
      ev.preventDefault();
      const box = this.svg.getBoundingClientRect();
      const mx = ev.clientX - box.left, my = ev.clientY - box.top;
      const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
      const scale = Math.min(4, Math.max(0.15, this.scale * factor));
      this.tx = mx - (mx - this.tx) * (scale / this.scale);
      this.ty = my - (my - this.ty) * (scale / this.scale);
      this.scale = scale;
      this.place();
    }, {passive: false});
  }

  fit() {
    const shown = this.nodes.filter(n => this.isShown(n));
    if (!shown.length) return;
    const box = this.svg.getBoundingClientRect();
    const width = box.width || 800, height = box.height || 560;
    const xs = shown.map(n => n.x), ys = shown.map(n => n.y);
    const minX = Math.min(...xs) - 60, maxX = Math.max(...xs) + 60, minY = Math.min(...ys) - 50, maxY = Math.max(...ys) + 60;
    const whole = Math.min(width / (maxX - minX), height / (maxY - minY));
    // Never shrink below a readable size; centre on the selected node instead.
    this.scale = Math.min(1.6, Math.max(0.6, whole));
    const anchor = whole < 0.6 && this.selected ? this.byId.get(this.selected) : null;
    const cx = anchor ? anchor.x : (minX + maxX) / 2, cy = anchor ? anchor.y : (minY + maxY) / 2;
    this.tx = width / 2 - cx * this.scale;
    this.ty = height / 2 - cy * this.scale;
    this.place();
  }

  centerOn(node) {
    const box = this.svg.getBoundingClientRect();
    this.tx = (box.width || 800) / 2 - node.x * this.scale;
    this.ty = (box.height || 560) / 2 - node.y * this.scale;
    this.place();
  }

  matches(node, text) {
    const query = (text || "").trim().toLowerCase();
    return query && ((node.label || "").toLowerCase().includes(query) || node.id.toLowerCase().includes(query));
  }

  highlight(text) {
    for (const [id, group] of this.groups) {
      group.classList.toggle("hit", Boolean(this.matches(this.byId.get(id), text)));
    }
  }

  reveal(ids) { for (const id of ids) this.visible.add(id); this.render(); }

  expand(id) {
    this.reveal([id, ...this.neighbours.get(id).map(e => e.source === id ? e.target : e.source)]);
    this.select(id);
  }

  focus(hops) {
    if (!this.selected) return;
    const keep = new Set([this.selected]);
    let frontier = [this.selected];
    for (let h = 0; h < hops; h++) {
      const next = [];
      for (const id of frontier) for (const e of this.neighbours.get(id)) {
        const other = e.source === id ? e.target : e.source;
        if (!keep.has(other)) { keep.add(other); next.push(other); }
      }
      frontier = next;
    }
    this.visible = keep;
    this.render();
    this.fit();
  }

  select(id) {
    this.selected = id;
    for (const [nid, group] of this.groups) group.classList.toggle("selected", nid === id);
    const node = this.byId.get(id);
    if (!node) return;
    const rows = [];
    for (const [key, value] of Object.entries(node)) {
      if (["x", "y", "dx", "dy", "id", "states"].includes(key) || value === "" || value === null || value === undefined) continue;
      rows.push(html("tr", {}, [html("th", {text: key}), html("td", {text: typeof value === "object" ? JSON.stringify(value) : String(value)})]));
    }
    if (node.states) {
      for (const [store, info] of Object.entries(node.states)) {
        rows.push(html("tr", {}, [html("th", {text: `state (${store})`}),
          html("td", {text: `${info.state}${info.inferred ? " (inferred)" : ""} → ${info.verdict} [${info.evidence_id} ${info.observed_at || ""}]`})]));
      }
    }
    const links = this.neighbours.get(id).map(e => {
      const other = this.byId.get(e.source === id ? e.target : e.source);
      const arrow = e.source === id ? "→" : "←";
      return html("li", {class: `kind-${e.kind}`}, [
        html("button", {type: "button", class: "linkbtn", text: `${arrow} ${other.label || other.id}`,
          onclick: () => { this.reveal([other.id]); this.select(other.id); this.centerOn(other); }}),
        html("div", {class: "small", text: `${e.relation} · ${e.kind}${e.time ? " · " + e.time : ""}`}),
        html("div", {class: "small muted", text: [e.evidence, e.note].filter(Boolean).join(" — ")}),
      ]);
    });
    this.panel.textContent = "";
    this.panel.append(
      html("h3", {text: node.label || node.id}),
      html("p", {class: `pill type-${node.type}`, text: TYPE_LABEL[node.type] || node.type}),
      html("table", {class: "kv"}, rows),
      html("h4", {text: `Connections (${links.length})`}),
      html("ul", {class: "glinks"}, links),
      html("div", {class: "gbuttons"}, [
        html("button", {type: "button", text: "Reveal neighbours", onclick: () => this.expand(id)}),
        html("button", {type: "button", text: "Focus here", onclick: () => this.focus(1)}),
        html("button", {type: "button", text: "Focus 2 steps", onclick: () => this.focus(2)}),
      ]),
    );
    if (this.options.onSelect) this.options.onSelect(node);
  }

  showEdge(e) {
    const a = this.byId.get(e.source), b = this.byId.get(e.target);
    this.panel.textContent = "";
    this.panel.append(html("h3", {text: e.relation}),
      html("p", {class: `pill kind-${e.kind}`, text: `${e.kind}: ${KIND_LABEL[e.kind] || ""}`}),
      html("table", {class: "kv"}, [
        ["from", a.label || a.id], ["to", b.label || b.id], ["evidence", e.evidence], ["time", e.time], ["note", e.note],
      ].filter(r => r[1]).map(([k, v]) => html("tr", {}, [html("th", {text: k}), html("td", {text: v})]))));
  }
}

window.GraphView = GraphView;
window.ghtml = html;
