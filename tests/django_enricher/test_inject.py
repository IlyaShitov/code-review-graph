import sqlite3

from code_review_graph.django_enricher.inject import apply
from code_review_graph.django_enricher.resolve import (
    RULE_CHAIN,
    RULE_CONSTANT,
    RULE_MRO,
    RULE_PERMISSION,
    ConstantNode,
    Resolved,
)


def test_apply_rewrites_inserts_and_is_idempotent(graph_db: sqlite3.Connection) -> None:
    resolved = [
        Resolved("/p/b.py", 3, "m", "/p/a.py::Q.m", RULE_CHAIN, "f"),
        Resolved("/p/b.py", 9, "n", "/p/a.py::Q.n", RULE_MRO, "f"),
        Resolved("/p/b.py", 5, "m", "/p/a.py::Q.m", RULE_PERMISSION, "f", via="F"),
        Resolved("/p/b.py", 11, "MAX_LIMIT", "/p/c.py::MAX_LIMIT", RULE_CONSTANT, "f"),
        Resolved("/p/b.py", 7, "x", "/p/a.py::Q.x", RULE_CHAIN, "f"),
    ]
    constants = [
        ConstantNode("/p/c.py::MAX_LIMIT", "MAX_LIMIT", "/p/c.py", 1, 1, "MAX_LIMIT = 5", "")
    ]
    class_docstrings = {"/p/a.py::Q": "name: Кью"}
    expected_edges = [
        ("CALLS", 3, "/p/b.py::f", "/p/a.py::Q.m", "direct", "EXTRACTED"),
        ("CALLS", 5, "/p/b.py::f", "/p/a.py::Q.m", "direct", "INFERRED"),
        ("CALLS", 9, "/p/b.py::f", "/p/a.py::Q.n", "direct", "EXTRACTED"),
        ("CONTAINS", 1, "/p/c.py", "/p/c.py::MAX_LIMIT", "direct", "INFERRED"),
        ("REFERENCES", 11, "/p/b.py::f", "/p/c.py::MAX_LIMIT", "direct", "INFERRED"),
    ]
    expected_nodes = [
        ("Class", "/p/a.py::Q", "ha", "name: Кью", None),
        ("Constant", "/p/c.py::MAX_LIMIT", "hc", "", "Max Limit"),
    ]
    edges_query = (
        "select kind, line, source_qualified, target_qualified, target_resolution, "
        "confidence_tier from edges "
        "order by kind, line"
    )
    nodes_query = (
        "select kind, qualified_name, file_hash, docstring, name_tokens from nodes where kind "
        "in ('Class', 'Constant') "
        "order by kind"
    )

    with graph_db:
        stats, _ = apply(graph_db, resolved, constants, class_docstrings)

    assert (stats["rewritten"], stats["no_target_node"]) == (2, 1)
    assert graph_db.execute(edges_query).fetchall() == expected_edges
    assert graph_db.execute(nodes_query).fetchall() == expected_nodes

    with graph_db:
        apply(graph_db, resolved, constants, class_docstrings)

    assert graph_db.execute(edges_query).fetchall() == expected_edges
    assert graph_db.execute(nodes_query).fetchall() == expected_nodes
