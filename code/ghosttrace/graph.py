"""Evidence graph: people, identifiers, records, processes and evidence.

The graph is built from the same observations, identity tables and activity
stream that the audit engine reads. Every edge says where it came from and
when, and whether it is:
- observed: a supplied record or snapshot shows it;
- claimed: a system reported it (activity stream, data catalog);
- derived: GhostTrace concluded it (identity resolution, workflow attribution,
  inferred absence, a destination read from a connector name).

Only exact identifiers connect a person to a record. Names and similar-looking
addresses never create an edge.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .ingest import default_system, iso_at, normalize


@dataclass
class Edge:
    source: str
    target: str
    relation: str
    kind: str              # observed, claimed or derived
    evidence_id: str       # file_id, activity_id, audit_id, or a source table name
    event_time: str = ""   # when the fact or event happened, as recorded
    captured_at: str = ""  # when the evidence holding it was captured
    note: str = ""

    def time(self) -> str:
        return self.event_time or self.captured_at

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "relation": self.relation,
                "kind": self.kind, "evidence_id": self.evidence_id, "event_time": self.event_time,
                "captured_at": self.captured_at, "note": self.note}


# Connector names that state which system a flow writes to. A name that does
# not mention a system leaves the destination unknown instead of guessed.
DESTINATION_WORDS = [("analytics", "analytics_platform"), ("search", "search_service"),
                     ("support", "support_platform"), ("marketing", "marketing_platform")]


def flow_destination(connector_name: str) -> str:
    name = connector_name.lower()
    for word, system in DESTINATION_WORDS:
        if word in name:
            return system
    return ""


def record_node(resource: str) -> str:
    if resource.startswith(("data_flow_", "backup_")):
        return f"process:{resource}"
    return f"record:{resource}"


@dataclass
class EvidenceGraph:
    nodes: dict[str, dict[str, str]] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    outgoing: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    incoming: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))

    def add_node(self, node: str, node_type: str, label: str = "", **extra: str) -> None:
        if node not in self.nodes:
            self.nodes[node] = {"type": node_type, "label": label or node.split(":", 1)[-1], **extra}

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
        self.outgoing[edge.source].append(edge)
        self.incoming[edge.target].append(edge)

    # -- building ----------------------------------------------------------

    @classmethod
    def build(cls, engine: Any) -> "EvidenceGraph":
        graph = cls()
        graph._add_identities(engine.identity)
        graph._add_records(engine)
        graph._add_activity(engine)
        return graph

    def _identifier(self, kind: str, value: str) -> str:
        node = f"identifier:{kind}:{normalize(value)}"
        self.add_node(node, "identifier", f"{kind} {value}", field=kind)
        return node

    def _add_identities(self, identity: Any) -> None:
        for user_id, row in identity.current.items():
            person = f"person:{user_id}"
            self.add_node(person, "person", user_id)
            for kind in ("email", "phone", "email_hash"):
                if row.get(kind):
                    self.add_edge(Edge(person, self._identifier(kind, row[kind]), "HAS_IDENTIFIER", "observed",
                                       "user_identities.csv", event_time=iso_at(row["updated_at"]),
                                       note="current"))
        for row in identity.history:
            if row["field_name"] not in ("email", "phone", "email_hash"):
                continue
            person = f"person:{row['user_id']}"
            self.add_node(person, "person", row["user_id"])
            when = iso_at(row["event_time"])
            if row["old_value"]:
                self.add_edge(Edge(person, self._identifier(row["field_name"], row["old_value"]), "HAD_IDENTIFIER",
                                   "observed", row["audit_id"], event_time=when, note=f"used until {when}"))
            if row["new_value"]:
                self.add_edge(Edge(person, self._identifier(row["field_name"], row["new_value"]), "HAS_IDENTIFIER",
                                   "observed", row["audit_id"], event_time=when, note=f"used from {when}"))

    def _add_records(self, engine: Any) -> None:
        identifier_nodes = {node.split(":", 2)[2]: node for node, data in self.nodes.items()
                            if data["type"] == "identifier"}
        for resource, observations in engine.by_resource.items():
            node = record_node(resource)
            kind = "process" if node.startswith("process:") else "record"
            self.add_node(node, kind, resource, system=default_system(resource))
            for obs in observations:
                evidence = f"evidence:{obs.evidence_id}"
                self.add_node(evidence, "evidence", obs.evidence_id, path=obs.source_path)
                copy = f"copy:{resource}@{obs.store}"
                self.add_node(copy, "copy", f"{resource} in {obs.store}")
                self.add_edge(Edge(copy, node, "COPY_OF", "observed", obs.evidence_id,
                                   event_time=obs.time(), captured_at=obs.captured_at, note=obs.store))
                self.add_edge(Edge(evidence, copy, "OBSERVES", "derived" if obs.inferred else "observed",
                                   obs.evidence_id, event_time=obs.time(), captured_at=obs.captured_at,
                                   note=state_note(obs)))
                connector = str(obs.fields.get("connector_name", ""))
                if connector and flow_destination(connector):
                    target = f"system:{flow_destination(connector)}"
                    self.add_node(target, "system", flow_destination(connector))
                    self.add_edge(Edge(node, target, "CAN_WRITE_TO", "derived", obs.evidence_id,
                                       event_time=obs.time(), captured_at=obs.captured_at,
                                       note=f"from connector name {connector}"))
                for key in obs.keys:
                    ident = identifier_nodes.get(key)
                    if ident:
                        self.add_edge(Edge(ident, node, link_relation(resource), "observed", obs.evidence_id,
                                           event_time=obs.time(), captured_at=obs.captured_at,
                                           note=f"{obs.store}: {obs.location}"))
                    elif key.startswith("user_") and f"person:{key}" in self.nodes:
                        self.add_edge(Edge(f"person:{key}", node, "REFERENCED_BY", "observed", obs.evidence_id,
                                           event_time=obs.time(), captured_at=obs.captured_at,
                                           note=f"stable reference in {obs.location}"))
        # Links the engine accepted from the catalog, single-record documents and restore lineage.
        observed_pairs = {(e.source, e.target) for e in self.edges}
        for (resource, user), sources in engine.owner_sources.items():
            for source in sources:
                if not source.startswith("activity_"):
                    continue
                node = record_node(resource)
                self.add_node(node, "process" if node.startswith("process:") else "record", resource,
                              system=default_system(resource))
                event = engine.activity_by_id.get(source, {})
                relation = "RESTORED_INTO" if event.get("event_type") == "RESTORE_BACKUP" else "CATALOG_LINK"
                kind = "derived" if relation == "RESTORED_INTO" else "claimed"
                if (f"person:{user}", node) not in observed_pairs:
                    self.add_edge(Edge(f"person:{user}", node, relation, kind, source,
                                       event_time=iso_at(event.get("event_time", "")),
                                       note=event.get("details", "")))

    def _add_activity(self, engine: Any) -> None:
        """Every activity row is a reported claim attached to the resource it names."""
        for event in engine.activity:
            node = record_node(event["resource_id"])
            if node not in self.nodes:
                continue
            evidence = f"evidence:{event['activity_id']}"
            self.add_node(evidence, "evidence", event["activity_id"], path="system_activity.csv")
            self.add_edge(Edge(evidence, node, f"REPORTS_{event['event_type']}", "claimed", event["activity_id"],
                               event_time=iso_at(event["event_time"]),
                               note=f"{event['source_system']} -> {event['target_system']}: "
                                    f"{event['reported_result']}; {event['details']}"))

    def add_workflow_links(self, case_id: str, assigned: dict[str, str], activity_time: dict[str, str]) -> None:
        request = f"request:{case_id}"
        self.add_node(request, "request", case_id)
        for resource, activity_id in assigned.items():
            node = record_node(resource)
            self.add_node(node, "process" if node.startswith("process:") else "record", resource,
                          system=default_system(resource))
            self.add_edge(Edge(request, node, "WORKFLOW_TOUCHED", "derived", activity_id,
                               event_time=activity_time.get(activity_id, ""),
                               note="attributed to this request's deletion workflow by elimination"))

    # -- traversal ---------------------------------------------------------

    def identifiers_of(self, user_id: str) -> dict[str, list[Edge]]:
        """Identifier node -> edges that show the person held it (current or historical)."""
        found: dict[str, list[Edge]] = defaultdict(list)
        for edge in self.outgoing.get(f"person:{user_id}", []):
            if edge.relation in ("HAS_IDENTIFIER", "HAD_IDENTIFIER"):
                found[edge.target].append(edge)
        return found

    def identifier_terms(self, user_id: str) -> set[str]:
        terms = {normalize(user_id)}
        for node in self.identifiers_of(user_id):
            terms.add(node.split(":", 2)[2])
        return terms

    def people_for(self, value: str) -> dict[str, list[Edge]]:
        """Identity traversal backwards: which people held this exact identifier."""
        people: dict[str, list[Edge]] = defaultdict(list)
        for node, data in self.nodes.items():
            if data["type"] == "identifier" and node.split(":", 2)[2] == normalize(value):
                for edge in self.incoming.get(node, []):
                    if edge.relation in ("HAS_IDENTIFIER", "HAD_IDENTIFIER"):
                        people[edge.source.split(":", 1)[1]].append(edge)
        return people

    def links_to_person(self, user_id: str) -> dict[str, list[Edge]]:
        """Record or process node -> edges that tie it to the person."""
        links: dict[str, list[Edge]] = defaultdict(list)
        person = f"person:{user_id}"
        for ident in self.identifiers_of(user_id):
            for edge in self.outgoing.get(ident, []):
                if edge.relation in ("IDENTIFIES", "SELECTS", "ARCHIVES"):
                    links[edge.target].append(edge)
        for edge in self.outgoing.get(person, []):
            if edge.relation in ("REFERENCED_BY", "CATALOG_LINK", "RESTORED_INTO"):
                links[edge.target].append(edge)
        return links


def link_relation(resource: str) -> str:
    if resource.startswith("data_flow_"):
        return "SELECTS"      # the connector copies data selected by this identifier
    if resource.startswith("backup_"):
        return "ARCHIVES"     # the archive holds data stored under this identifier
    return "IDENTIFIES"


def state_note(obs: Any) -> str:
    text = obs.state
    if obs.state == "PRESENT" and obs.identifying is not None:
        text += " with direct identifiers" if obs.identifying else " without direct identifiers"
    if obs.inferred:
        text += " (inferred: missing from a snapshot that lists this store)"
    return f"{text} in {obs.store}"
