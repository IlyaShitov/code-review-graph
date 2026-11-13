"""Exact symbol names survive bounded candidate selection."""

from types import SimpleNamespace

import pytest

from code_review_graph import embeddings, search
from code_review_graph.parser import NodeInfo
from code_review_graph.uncertainty import empty_search_confidence


@pytest.mark.parametrize("backend", ["fts", "keyword"])
def test_kind_is_applied_before_candidate_limit(crowded_search_store, monkeypatch, backend):
    if backend == "keyword":
        monkeypatch.setattr(search, "_fts_search", lambda *args, **kwargs: [])
    result = search.hybrid_search(crowded_search_store, "Trip", kind="Class", limit=2)
    assert {node["name"] for node in result} == {"TripVehicle", "TripStop"}


@pytest.mark.parametrize("query", ["Trip", "trip", " Trip "])
@pytest.mark.parametrize("backend", ["fts", "keyword"])
def test_exact_names_are_first_even_outside_candidate_window(
    crowded_search_store, monkeypatch, query, backend,
):
    for index in range(2):
        crowded_search_store.upsert_node(NodeInfo(
            "Class", "Trip", f"/repo/very/long/path/{index}.py", 1, 200,
            extra={"docstring": "unrelated description " * 300},
        ))
    search.rebuild_fts_index(crowded_search_store)
    if backend == "keyword":
        monkeypatch.setattr(search, "_fts_search", lambda *args, **kwargs: [])
    result = search.hybrid_search(crowded_search_store, query, kind="Class", limit=2)
    assert [node["name"] for node in result] == ["Trip", "Trip"]
    assert len({node["qualified_name"] for node in result}) == 2


def test_exact_match_is_not_duplicated_and_fuzzy_results_fill_limit(crowded_search_store):
    crowded_search_store.upsert_node(NodeInfo("Class", "Trip", "/repo/exact.py", 1, 2))
    search.rebuild_fts_index(crowded_search_store)
    result = search.hybrid_search(crowded_search_store, "Trip", kind="Class", limit=3)
    assert result[0]["name"] == "Trip"
    assert {node["name"] for node in result} == {"Trip", "TripVehicle", "TripStop"}
    assert len(result) == 3


def test_exact_lookup_does_not_require_fts(search_store):
    search_store.upsert_node(NodeInfo("Class", "Trip", "/repo/exact.py", 1, 2))
    result = search.hybrid_search(search_store, "Trip", kind="Class", limit=1)
    assert [node["name"] for node in result] == ["Trip"]


def test_embedding_kind_is_filtered_before_top_k(crowded_search_store, monkeypatch):
    provider = SimpleNamespace(name="test", embed_query=lambda query: [1.0, 0.0])
    monkeypatch.setattr(embeddings, "get_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(search, "_fts_search", lambda *args, **kwargs: [])
    with embeddings.EmbeddingStore(crowded_search_store.db_path) as vectors:
        for row in crowded_search_store._conn.execute("SELECT qualified_name, kind FROM nodes"):
            vector = [1.0, 0.0] if row["kind"] == "Function" else [0.5, 0.5]
            vectors._conn.execute(
                "INSERT INTO embeddings VALUES (?, ?, ?, ?)",
                (row["qualified_name"], embeddings._encode_vector(vector), "test", "test"),
            )
    result = search.hybrid_search(crowded_search_store, "bus", kind="Class", limit=2)
    assert {node["name"] for node in result} == {"TripVehicle", "TripStop"}


@pytest.mark.parametrize("query, kind, limit", [
    ("NonexistentSymbol", "Class", 2), ("Trip", "Constant", 2),
    ("Trip", "Class", 0), ("", "Class", 2), ("' OR 1=1 --", "Class", 2),
])
def test_empty_and_invalid_searches_do_not_leak_results(crowded_search_store, query, kind, limit):
    assert search.hybrid_search(crowded_search_store, query, kind=kind, limit=limit) == []


def test_empty_message_does_not_claim_symbol_absence(crowded_search_store, tmp_path):
    note = empty_search_confidence(crowded_search_store, tmp_path, "Trip")
    assert "no indexed node matches" not in note
    assert "not proof of absence" in note
    assert len(note) <= 140


def test_empty_graph_message_still_explains_indexing(search_store, tmp_path):
    assert "graph is empty" in empty_search_confidence(search_store, tmp_path, "Trip")

