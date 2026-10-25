"""Per-case views of the evidence graph and timeline at a request's audit_at.

These views only read what the audit engine and graph already hold. They never
infer a missing event or a cause from timing: an event is listed as recorded,
and an observed state is listed with both its observation and capture times.
"""

from __future__ import annotations

from typing import Any

from .graph import Edge, flow_destination, record_node
from .ingest import iso_at


def resource_of(node: str) -> str:
    return node.split(":", 1)[1]


def connector_names(engine: Any, resource: str, cutoff: str) -> list[str]:
    return sorted({str(o.fields["connector_name"]) for o in engine.by_resource.get(resource, [])
                   if o.fields.get("connector_name") and o.captured_at <= cutoff})


def process_state(record: dict[str, Any]) -> tuple[str, str, str]:
    """(state, evidence_id, observed_at) of the store that keeps a process open, if any."""
    stores = record.get("stores", {})
    for verdict in ("violation", "unknown", "ok"):
        for info in stores.values():
            if info["verdict"] == verdict:
                return info["state"], info["evidence_id"], info["observed_at"]
    return "NOT OBSERVED", "", ""


def recreation_paths(engine: Any, detail: dict[str, Any], cutoff: str,
                     states: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Active sync or restore processes tied to the person, with what they could recreate.

    A path is reported only when supplied evidence shows the process open at the
    cutoff: an enabled connector or a restorable archive. `states` lets the
    what-if analysis override a process state hypothetically.
    """
    records = {r["resource_id"]: r for r in detail.get("resources", [])}
    paths = []
    for resource, record in sorted(records.items()):
        if not resource.startswith(("data_flow_", "backup_")):
            continue
        state, evidence_id, observed_at = process_state(record)
        if states and resource in states:
            state, evidence_id, observed_at = states[resource], "hypothetical", ""
        if state not in ("ACTIVE", "RESTORABLE"):
            continue
        path: dict[str, Any] = {"process": resource, "state": state, "state_evidence": evidence_id,
                                "observed_at": observed_at, "linked_by": record["linked_by"]}
        if resource.startswith("data_flow_"):
            names = connector_names(engine, resource, cutoff)
            destinations = sorted({flow_destination(n) for n in names if flow_destination(n)})
            path["kind"] = "sync"
            path["connector_names"] = names
            path["destination_systems"] = destinations
            path["destination_basis"] = ("derived from connector name" if destinations
                                         else "destination not stated in the supplied evidence")
            path["can_recreate"] = sorted(r for r, rec in records.items()
                                          if rec["system_id"] in destinations
                                          and rec["target"] in ("ABSENT", "REDACTED", "ANONYMIZED"))
        else:
            path["kind"] = "restore"
            path["destination_basis"] = "archive contents are not itemized; any archived copy is at risk"
            path["can_recreate"] = sorted(r for r, rec in records.items()
                                          if rec["target"] in ("ABSENT", "REDACTED", "ANONYMIZED"))
            path["recorded_restores"] = sorted(
                f"{e['activity_id']}: {e['details']}" for e in engine.activity
                if e["event_type"] == "RESTORE_BACKUP" and e["resource_id"] == resource
                and iso_at(e["event_time"]) <= cutoff)
        paths.append(path)
    return paths


def edge_view(edge: Edge, cutoff: str) -> dict[str, str]:
    view = edge.as_dict()
    view["after_audit_at"] = "yes" if (edge.captured_at or edge.event_time) > cutoff else ""
    return view


def case_graph(engine: Any, request: dict[str, str], include_later: bool = False) -> dict[str, Any]:
    """The focused graph for one request at its audit_at."""
    cutoff = iso_at(request["audit_at"])
    row, detail = engine.investigate(request)
    view: dict[str, Any] = {"case_id": request["case_id"], "audit_at": cutoff,
                            "audit_status": row["deletion_status"], "intent": detail.get("intent"),
                            "person": detail.get("user_id"), "identity": [], "records": [],
                            "recreation_paths": [], "excluded": {}}
    if row["deletion_status"] == "NOT_APPLICABLE" or not detail.get("user_id"):
        view["excluded"]["reason"] = detail.get("uncertain") or detail.get("intent")
        return view
    user = detail["user_id"]
    graph = engine.graph
    # Identity traversal: request email -> person -> every identifier they held.
    start = graph.people_for(request["email"])
    view["identity_start"] = {"request_email": request["email"],
                              "matched_people": {p: [e.evidence_id for e in edges] for p, edges in start.items()}}
    later = 0
    for ident, edges in sorted(graph.identifiers_of(user).items()):
        for edge in edges:
            if edge.event_time > cutoff and not include_later:
                later += 1
                continue
            view["identity"].append({"identifier": graph.nodes[ident]["label"], "relation": edge.relation,
                                     "kind": edge.kind, "evidence_id": edge.evidence_id,
                                     "event_time": edge.event_time, "note": edge.note})
    links = graph.links_to_person(user)
    workflow = {e.target: e for e in graph.outgoing.get(f"request:{request['case_id']}", [])}
    for record in detail["resources"]:
        resource = record["resource_id"]
        node = record_node(resource)
        link_edges, seen = [], set()
        for edge in links.get(node, []) + ([workflow[node]] if node in workflow else []):
            if (edge.relation, edge.evidence_id) not in seen:
                seen.add((edge.relation, edge.evidence_id))
                link_edges.append(edge_view(edge, cutoff))
        claims = [edge_view(e, cutoff) for e in graph.incoming.get(node, [])
                  if e.kind == "claimed" and (include_later or e.time() <= cutoff)]
        observed = []
        copies = {e.source for e in graph.incoming.get(node, []) if e.relation == "COPY_OF"}
        for copy_node in sorted(copies):
            for obs_edge in graph.incoming.get(copy_node, []):
                if obs_edge.relation != "OBSERVES":
                    continue
                if obs_edge.captured_at <= cutoff or include_later:
                    observed.append(edge_view(obs_edge, cutoff))
                else:
                    later += 1
        later += sum(1 for e in graph.incoming.get(node, []) if e.kind == "claimed" and e.time() > cutoff)
        view["records"].append({
            "resource_id": resource, "node_type": engine.graph.nodes.get(node, {}).get("type", "record"),
            "system_id": record["system_id"], "category": record["category"],
            "policy": record["required"], "required_state": record["target"],
            "state_at_audit": {s: {k: v for k, v in info.items()} for s, info in record["stores"].items()},
            "linked_by": link_edges, "observations": sorted(observed, key=lambda e: (e["event_time"], e["evidence_id"])),
            "claims": sorted(claims, key=lambda e: e["event_time"]),
            "destinations": [edge_view(e, cutoff) for e in graph.outgoing.get(node, []) if e.relation == "CAN_WRITE_TO"],
        })
    view["recreation_paths"] = recreation_paths(engine, detail, cutoff)
    view["excluded"] = {
        "edges_after_audit_at": later,
        "earlier_cycle_resources": detail.get("earlier_cycle_resources", []),
        "resources_first_seen_after_audit_at": detail.get("later_resources", []),
        "workflow_events_tied_with_other_requests": detail.get("unassigned_workflow_resources", []),
    }
    return view


def case_timeline(engine: Any, request: dict[str, str], include_later: bool = False) -> dict[str, Any]:
    """Recorded events and observed states for one request, in time order."""
    cutoff = iso_at(request["audit_at"])
    row, detail = engine.investigate(request)
    entries: list[dict[str, str]] = [
        {"time": iso_at(request["requested_at"]), "type": "request", "what": "deletion request submitted",
         "source": "request row", "captured_at": ""},
        {"time": iso_at(request["reported_at"]), "type": "reported claim",
         "what": f"deletion workflow reported {request['reported_status']}", "source": "request row",
         "captured_at": ""},
        {"time": cutoff, "type": "audit cutoff", "what": "state is judged here; later evidence is not used",
         "source": "request row", "captured_at": ""},
    ]
    user = detail.get("user_id")
    if user:
        for change in engine.identity.identity_evidence(user):
            entries.append({"time": iso_at(change["event_time"]), "type": "recorded event",
                            "what": f"{change['field_name']} changed from {change['old_value']} to {change['new_value']}",
                            "source": change["audit_id"], "captured_at": ""})
    resources = [r["resource_id"] for r in detail.get("resources", [])]
    for resource in resources:
        for event in engine.activity:
            if event["resource_id"] == resource:
                entries.append({"time": iso_at(event["event_time"]), "type": "reported claim",
                                "what": f"{event['event_type']} {resource} by {event['source_system']}: "
                                        f"{event['reported_result']} ({event['details']})",
                                "source": event["activity_id"], "captured_at": ""})
        for obs in engine.by_resource.get(resource, []):
            if obs.method == "activity":
                continue   # already listed above as the activity row that reported it
            state = obs.state
            if obs.state == "PRESENT" and obs.identifying is not None:
                state += " with direct identifiers" if obs.identifying else " without direct identifiers"
            entries.append({"time": obs.time(), "type": "observed state",
                            "what": f"{resource} {state} in {obs.store}", "source": obs.evidence_id,
                            "captured_at": obs.captured_at})
    kept, hidden = [], 0
    for entry in entries:
        late = entry["time"] > cutoff or (entry["captured_at"] and entry["captured_at"] > cutoff)
        if late and not include_later:
            hidden += 1
            continue
        entry["after_audit_at"] = "yes" if late else ""
        kept.append(entry)
    order = {"request": 0, "recorded event": 1, "reported claim": 2, "observed state": 3, "audit cutoff": 4}
    kept.sort(key=lambda e: (e["time"], order[e["type"]], e["source"]))
    return {"case_id": request["case_id"], "audit_at": cutoff, "audit_status": row["deletion_status"],
            "person": user, "entries": kept, "hidden_after_audit_at": hidden,
            "note": "Events are listed as recorded and states as observed; no cause is inferred from order."}


# ---------------------------------------------------------------------------
# Plain-text rendering for the terminal.

def render_graph(view: dict[str, Any]) -> str:
    out = [f"EVIDENCE GRAPH  {view['case_id']}  at {view['audit_at']}  (supplied evidence only)",
           f"audit verdict: {view['audit_status']}    person: {view.get('person')}"]
    if not view.get("person") or view["audit_status"] == "NOT_APPLICABLE":
        out.append(f"no graph: {view['excluded'].get('reason')}")
        return "\n".join(out)
    start = view["identity_start"]
    out.append(f"\nIDENTITY  request email {start['request_email']} -> {', '.join(start['matched_people'])}")
    for item in view["identity"]:
        out.append(f"  [{item['kind']}] {item['relation']:<15} {item['identifier']:<45} {item['evidence_id']} "
                   f"{item['event_time']}  {item['note']}")
    out.append("\nRECORDS AND PROCESSES")
    for rec in view["records"]:
        out.append(f"  {rec['node_type']} {rec['system_id']}/{rec['resource_id']}  {rec['category']}  "
                   f"policy {rec['policy'] or '-'} -> must be {rec['required_state']}")
        for link in rec["linked_by"]:
            late = ("  <- captured after audit_at: shows the record existed, never used for its state"
                    if link["after_audit_at"] else "")
            out.append(f"    link [{link['kind']}] {link['relation']} via {link['evidence_id']} "
                       f"(event {link['event_time'] or '-'}, captured {link['captured_at'] or '-'}){late}")
        for store, info in rec["state_at_audit"].items():
            tag = " inferred" if info["inferred"] else ""
            out.append(f"    state {store}: {info['state']}{tag} identifying={info['identifying']} "
                       f"-> {info['verdict']}  [{info['evidence_id']} observed {info['observed_at']}]")
        if not rec["state_at_audit"]:
            out.append("    state: not observed at or before audit_at")
        for dest in rec["destinations"]:
            out.append(f"    writes to [{dest['kind']}] {dest['target'].split(':', 1)[1]} ({dest['note']})")
        if rec["claims"]:
            out.append(f"    claims: {len(rec['claims'])} reported activity rows (see timeline)")
    out.append("\nRECREATION PATHS (supported by evidence at audit_at)")
    if not view["recreation_paths"]:
        out.append("  none")
    for path in view["recreation_paths"]:
        target = ", ".join(path.get("destination_systems") or []) or path["destination_basis"]
        out.append(f"  {path['kind']} {path['process']} {path['state']} [{path['state_evidence']}] -> {target}")
        out.append(f"    could recreate: {', '.join(path['can_recreate']) or 'nothing else linked in this case'}")
        if path.get("recorded_restores"):
            out.append(f"    recorded restores (claims): {', '.join(path['recorded_restores'])}")
    ex = view["excluded"]
    out.append("\nNOT USED")
    out.append(f"  observations, claims and identity edges after audit_at: {ex['edges_after_audit_at']}")
    for key in ("earlier_cycle_resources", "resources_first_seen_after_audit_at",
                "workflow_events_tied_with_other_requests"):
        if ex.get(key):
            out.append(f"  {key.replace('_', ' ')}: {', '.join(ex[key])}")
    return "\n".join(out)


def render_timeline(view: dict[str, Any]) -> str:
    out = [f"TIMELINE  {view['case_id']}  audit_at {view['audit_at']}  verdict {view['audit_status']}",
           f"{'time':<21} {'type':<15} {'source':<13} {'captured_at':<21} what"]
    for e in view["entries"]:
        mark = " (after audit_at)" if e.get("after_audit_at") else ""
        out.append(f"{e['time']:<21} {e['type']:<15} {e['source']:<13} {e['captured_at'] or '-':<21} {e['what']}{mark}")
    if view["hidden_after_audit_at"]:
        out.append(f"... {view['hidden_after_audit_at']} entries after audit_at hidden (use --all)")
    out.append(view["note"])
    return "\n".join(out)
