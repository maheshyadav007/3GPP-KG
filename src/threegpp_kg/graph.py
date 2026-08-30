from __future__ import annotations

from dataclasses import dataclass

from .constants import EdgeType


@dataclass(frozen=True, slots=True)
class GraphFact:
    source_type: str
    source_id: str
    predicate: str
    target_type: str
    target_id: str


def validate_graph(nodes: set[tuple[str, str]], edges: list[GraphFact]) -> list[str]:
    errors: list[str] = []
    for edge in edges:
        if (edge.source_type, edge.source_id) not in nodes:
            errors.append(f"orphan source {edge.source_type}:{edge.source_id} for {edge.predicate}")
        if (edge.target_type, edge.target_id) not in nodes:
            errors.append(f"orphan target {edge.target_type}:{edge.target_id} for {edge.predicate}")
        if edge.predicate == EdgeType.REVISES and (
            edge.source_type != "tdoc" or edge.target_type != "tdoc"
        ):
            errors.append("revises edges must connect TDocs")
    revision_graph: dict[str, set[str]] = {}
    for edge in edges:
        if edge.predicate == EdgeType.REVISES:
            revision_graph.setdefault(edge.source_id, set()).add(edge.target_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> bool:
        if identifier in visiting:
            return True
        if identifier in visited:
            return False
        visiting.add(identifier)
        cyclic = any(visit(target) for target in revision_graph.get(identifier, set()))
        visiting.remove(identifier)
        visited.add(identifier)
        return cyclic

    if any(visit(identifier) for identifier in revision_graph):
        errors.append("revision graph contains a cycle")
    return errors
