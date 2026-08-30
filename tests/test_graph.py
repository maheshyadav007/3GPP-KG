from threegpp_kg.constants import EdgeType
from threegpp_kg.graph import GraphFact, validate_graph


def test_graph_validation_detects_orphans_direction_and_revision_cycles() -> None:
    nodes = {("tdoc", "a"), ("tdoc", "b")}
    edges = [
        GraphFact("tdoc", "a", EdgeType.REVISES, "tdoc", "b"),
        GraphFact("tdoc", "b", EdgeType.REVISES, "tdoc", "a"),
        GraphFact("meeting", "missing", EdgeType.CONTAINS, "tdoc", "a"),
        GraphFact("topic", "x", EdgeType.REVISES, "tdoc", "a"),
    ]
    errors = validate_graph(nodes, edges)
    assert errors == [
        "orphan source meeting:missing for contains",
        "orphan source topic:x for revises",
        "revises edges must connect TDocs",
        "revision graph contains a cycle",
    ]


def test_graph_validation_accepts_well_formed_revision_chain() -> None:
    nodes = {("tdoc", "a"), ("tdoc", "b"), ("meeting", "m")}
    edges = [
        GraphFact("meeting", "m", EdgeType.CONTAINS, "tdoc", "a"),
        GraphFact("tdoc", "b", EdgeType.REVISES, "tdoc", "a"),
    ]
    assert validate_graph(nodes, edges) == []


def test_graph_validation_reports_orphan_target_without_false_cycle() -> None:
    errors = validate_graph(
        {("tdoc", "a")},
        [GraphFact("tdoc", "a", EdgeType.REVISES, "tdoc", "missing")],
    )
    assert errors == ["orphan target tdoc:missing for revises"]
