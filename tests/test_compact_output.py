"""Native MCP text output replaces the client-specific crg-compact hook."""

import asyncio
import copy
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from code_review_graph import main


@pytest.fixture
def query_response():
    node = {
        "name": "run", "parent_name": "Worker", "kind": "Function",
        "qualified_name": "/repo/pkg/jobs.py::Worker.run",
        "file_path": "/repo/pkg/jobs.py", "line_start": 4, "line_end": 20,
    }
    return {
        "status": "ok", "pattern": "callers_of", "target": "/repo/pkg/api.py::send",
        "summary": "Found 2 call site(s) across 1 node(s)",
        "result_count": 2, "distinct_nodes": 1, "results_omitted": 0,
        "resolution_split": {"direct": 1, "unresolved": 1},
        "results": [
            {**node, "call_site": {"line": 8}},
            {**node, "call_site": {"file": "/repo/generated.py", "line": 12},
             "target_resolution": "unresolved"},
        ],
        "edges": [
            {"source": node["qualified_name"], "target": "/repo/pkg/api.py::send",
             "kind": "CALLS", "file_path": "/repo/pkg/jobs.py", "line": 8,
             "confidence_tier": "EXTRACTED", "confidence": 1.0},
            {"source": node["qualified_name"], "target": "send",
             "kind": "CALLS", "file_path": "/repo/generated.py", "line": 12,
             "confidence_tier": "INFERRED", "confidence": 0.5,
             "target_resolution": "unresolved"},
        ],
        "confidence": "Some targets are only name matches.",
        "_graph": {"head_matches_build": False, "built_at_sha": "old", "head_sha": "new"},
    }


@pytest.fixture
def search_response(query_response):
    return {
        "status": "ok", "query": "run", "search_mode": "fts",
        "summary": "Found 1 node(s) matching 'run'", "result_count": 1,
        "total": 1, "truncated": False, "results_omitted": 0,
        "results": [{
            **query_response["results"][0], "is_test": True,
            "signature": "def run(self):", "docstring": "First line\n   second line",
            "source": "def run(self):\n    send()", "source_lines": [4, 5],
            "source_truncated": True,
            "callees": [{"qualified_name": "/repo/pkg/api.py::send", "kind": "Function",
                         "source": "def send():\n    pass", "source_lines": [1, 2]}],
        }],
        "source_omitted": 2,
    }


def test_query_defaults_to_compact_text(monkeypatch, query_response):
    monkeypatch.setattr(main, "query_graph", lambda **kwargs: query_response)
    result = asyncio.run(main.query_graph_tool("callers_of", "send", repo_root="/repo"))
    assert isinstance(result, str)
    assert "pkg/jobs.py:8" in result
    assert "generated.py:12" in result
    assert "[definition: pkg/jobs.py::Worker.run]" in result
    assert "Worker.run" in result
    assert "INFERRED" in result and "unresolved" in result
    assert "Some targets are only name matches." in result
    assert "old" in result and "new" in result


def test_compaction_keeps_sources_and_neighbors(search_response):
    from code_review_graph.compact import format_compact

    text = format_compact(search_response, "/repo")
    assert "search_mode: fts" in text
    assert "def run(self):\n    send()" in text
    assert "def send():\n    pass" in text
    assert "source_truncated" in text and "source_omitted: 2" in text
    assert "First line second line" in text
    assert "test" in text


def test_compaction_keeps_ambiguous_candidates_and_counts(query_response):
    from code_review_graph.compact import format_compact

    response = {
        "status": "ambiguous", "summary": "Choose a qualified_name",
        "candidates": query_response["results"], "candidate_count": 8,
        "candidates_omitted": 6, "endpoint": "to",
    }
    text = format_compact(response, "/repo")
    assert "/repo/pkg/jobs.py::Worker.run" in text
    assert "candidate_count: 8" in text and "candidates_omitted: 6" in text
    assert "endpoint: to" in text


def test_ambiguity_does_not_repeat_the_same_candidate_as_json(query_response):
    from code_review_graph.compact import format_compact

    candidates = query_response["results"][:1]
    response = {
        "status": "ambiguous", "summary": "Choose a qualified_name",
        "candidates": candidates, "disambiguation": candidates,
    }
    text = format_compact(response, "/repo")
    assert text.count(candidates[0]["qualified_name"]) == 1


def test_route_keeps_endpoints_certainty_and_weakest_hop():
    from code_review_graph.compact import format_compact

    hop = {"from": "/repo/a.py::run", "to": "/repo/b.py::send", "kind": "CALLS",
           "file_path": "/repo/a.py", "line": 9, "certainty": "shared_name"}
    response = {
        "status": "ok", "pattern": "path_to", "target": hop["from"], "to": hop["to"],
        "summary": "Found a route", "result_count": 1, "results": [hop],
        "certainty": "shared_name", "weakest_hop": hop,
    }
    text = format_compact(response, "/repo")
    assert "a.py::run -> b.py::send" in text
    assert "a.py:9" in text
    assert "shared_name" in text and "weakest_hop" in text


@pytest.mark.parametrize("response", [
    {"status": "error", "error": "bad pattern"},
    {"status": "not_found", "summary": "not indexed"},
    {"status": "ok"},
    {"status": "ok", "results": [{"unsupported": "shape"}]},
])
def test_unsupported_responses_are_unchanged(response):
    from code_review_graph.compact import format_compact

    assert format_compact(response, "/repo") is response


def test_empty_and_limited_results_keep_explanations():
    from code_review_graph.compact import format_compact

    response = {
        "status": "ok", "pattern": "callers_of", "target": "map", "results": [],
        "summary": "Common builtin skipped", "confidence": "Not proof of absence",
        "results_omitted": 9, "search_incomplete": True,
    }
    text = format_compact(response, "/repo")
    assert "Common builtin skipped" in text
    assert "Not proof of absence" in text
    assert "results_omitted: 9" in text and "search_incomplete: true" in text


def test_callee_edge_confidence_and_inferred_tests_are_preserved(query_response):
    from code_review_graph.compact import format_compact

    response = copy.deepcopy(query_response)
    response["pattern"] = "callees_of"
    row = response["results"][1]
    edge = response["edges"][1]
    edge["target"] = row["qualified_name"]
    edge["source"] = "/repo/other.py::caller"
    row["inferred_by"] = "name_match"
    row["indirect"] = True
    text = format_compact(response, "/repo")
    assert "INFERRED" in text and "name_match" in text and "indirect" in text


def test_file_caller_and_resolver_rule_are_spelled_out(query_response):
    """A file caller is named by its call site; a resolver rule replaces the tier."""
    from code_review_graph.compact import format_compact

    response = copy.deepcopy(query_response)
    path = "/repo/pkg/urls.py"
    response["results"][0] = {
        "name": path, "kind": "File", "qualified_name": path, "file_path": path,
        "line_start": 1, "line_end": 30, "call_site": {"line": 8},
    }
    response["edges"][0] |= {
        "source": path, "file_path": path, "confidence_tier": "INFERRED", "confidence": 0.8,
        "rule": "constant_ref",
    }
    lines = format_compact(response, "/repo").splitlines()
    assert "pkg/urls.py:8  (File 1-30, CALLS, constant_ref)" in lines


def test_formatter_does_not_mutate_response(query_response):
    from code_review_graph.compact import format_compact

    before = copy.deepcopy(query_response)
    format_compact(query_response, "/repo")
    assert query_response == before


@pytest.mark.parametrize("tool_name,impl_name,args,response_name", [
    ("query_graph_tool", "query_graph", {"pattern": "callers_of", "target": "send"},
     "query_response"),
    ("semantic_search_nodes_tool", "semantic_search_nodes", {"query": "run"},
     "search_response"),
])
async def test_mcp_wire_formats(monkeypatch, request, tool_name, impl_name, args, response_name):
    response = request.getfixturevalue(response_name)
    monkeypatch.setattr(main, impl_name, lambda **kwargs: response)
    monkeypatch.setattr(main, "with_provenance", lambda result, root: result)
    async with Client(main.mcp) as client:
        compact = await client.call_tool(tool_name, {**args, "repo_root": "/repo"})
        assert compact.structured_content is None
        assert len(compact.content) == 1
        assert "Worker.run" in compact.content[0].text
        full = await client.call_tool(tool_name, {**args, "output_format": "json"})
        assert full.structured_content == response
        assert json.loads(full.content[0].text) == response
        with pytest.raises(ToolError, match="output_format"):
            await client.call_tool(tool_name, {**args, "output_format": "invalid"})


def test_minimal_rows_and_unattached_edges_keep_their_locations():
    from code_review_graph.compact import format_compact

    response = {
        "status": "ok", "pattern": "callees_of", "summary": "Found 1 result",
        "results": [{"name": "run", "kind": "Function", "file_path": "/repo/a.py",
                     "call_site": {"line": 3}, "target_resolution": "unresolved"}],
        "edges": [{"source": "/repo/a.py::run", "target": "external",
                   "kind": "CALLS", "file_path": "/repo/a.py", "line": 5,
                   "confidence_tier": "INFERRED", "unresolved_targets": ["external"]}],
    }
    text = format_compact(response, "/repo")
    assert "a.py:3  run" in text
    assert "a.py:5" in text and "external" in text and "INFERRED" in text
    assert "None" not in text


def test_missing_identity_is_not_fabricated():
    from code_review_graph.compact import format_compact

    response = {
        "status": "ok", "summary": "Found 1 result", "results": [{
            "name": "handle", "kind": "Function", "file_path": "/repo/jobs.py",
        }],
    }
    text = format_compact(response, "/repo")
    assert "jobs.py" in text and "handle" in text
    assert "::" not in text


@pytest.mark.parametrize("name", ["all", "filter", "Q"])
def test_unresolved_calls_do_not_claim_a_definition(name):
    from code_review_graph.compact import format_compact

    response = {
        "status": "ok", "summary": "Found 1 call", "results": [{
            "name": name, "qualified_name": name, "kind": "Function",
            "target_resolution": "unresolved", "call_site": {"file": "/repo/jobs.py", "line": 8},
        }],
    }
    text = format_compact(response, "/repo")
    assert f"jobs.py:8  {name}" in text
    assert "unresolved" in text
    assert "definition:" not in text


async def test_minimal_method_identity_can_be_reused(tmp_path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build, get_db_path

    (tmp_path / ".git").mkdir()
    (tmp_path / "jobs.py").write_text(
        "class Worker:\n    def handle(self):\n        pass\n"
        "    def dispatch(self):\n        self.handle()\n",
        encoding="utf-8",
    )
    with GraphStore(get_db_path(tmp_path)) as store:
        full_build(tmp_path, store)
    args = {"repo_root": str(tmp_path), "detail_level": "minimal"}
    async with Client(main.mcp) as client:
        search = await client.call_tool(
            "semantic_search_nodes_tool",
            {**args, "query": "Worker.handle", "output_format": "json"},
        )
        qualified = search.structured_content["results"][0]["qualified_name"]
        assert qualified == f"{tmp_path}/jobs.py::Worker.handle"
        text = await client.call_tool(
            "semantic_search_nodes_tool", {**args, "query": "Worker.handle"},
        )
        assert "jobs.py::Worker.handle" in text.content[0].text
        callers = await client.call_tool(
            "query_graph_tool",
            {**args, "pattern": "callers_of", "target": qualified, "output_format": "json"},
        )
        assert callers.structured_content["results"][0]["qualified_name"] == (
            f"{tmp_path}/jobs.py::Worker.dispatch"
        )
        text = await client.call_tool(
            "query_graph_tool", {**args, "pattern": "callers_of", "target": qualified},
        )
        assert "Worker.dispatch" in text.content[0].text


@pytest.mark.parametrize("row", [
    {"import_target": "/repo/vendor", "import_target_kind": "directory"},
    {"importer": "/repo/a.py", "file": "/repo/a.py"},
])
def test_import_records_have_real_labels(row):
    from code_review_graph.compact import format_compact

    response = {
        "status": "ok", "summary": "Found 1 import", "results": [row],
        "edges": [{"source": "/repo/a.py", "target": "/repo/vendor",
                   "kind": "IMPORTS_FROM", "file_path": "/repo/a.py", "line": 3}],
    }
    text = format_compact(response, "/repo")
    assert "\n::" not in text
    assert "a.py:3" in text and "IMPORTS_FROM" in text
    assert "edge:" not in text
    if "import_target_kind" in row:
        assert "vendor" in text and "directory" in text


@pytest.mark.parametrize("detail_level", ["standard", "minimal"])
async def test_real_graph_over_mcp(tmp_path, detail_level):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build, get_db_path

    (tmp_path / ".git").mkdir()
    (tmp_path / "jobs.py").write_text(
        "def send():\n    pass\n\ndef run():\n    send()\n    send()\n",
        encoding="utf-8",
    )
    with GraphStore(get_db_path(tmp_path)) as store:
        full_build(tmp_path, store)
    args = {"repo_root": str(tmp_path), "detail_level": detail_level}
    async with Client(main.mcp) as client:
        callers = await client.call_tool(
            "query_graph_tool",
            {**args, "pattern": "callers_of", "target": f"{tmp_path}/jobs.py::send"},
        )
        text = callers.content[0].text
        assert "jobs.py:5" in text and "jobs.py:6" in text
        route = await client.call_tool(
            "query_graph_tool", {**args, "pattern": "path_to", "target": "run", "to": "send"},
        )
        assert "jobs.py::run -> jobs.py::send" in route.content[0].text
        search = await client.call_tool(
            "semantic_search_nodes_tool",
            {**args, "query": "run", "include_source": True, "expand_neighbors": True},
        )
        assert "jobs.py::run" in search.content[0].text
        if detail_level == "standard":
            assert "def run():\n    send()\n    send()" in search.content[0].text
            assert "def send():\n    pass" in search.content[0].text
        error = await client.call_tool(
            "query_graph_tool", {**args, "pattern": "unknown", "target": "run"},
        )
        assert error.structured_content["status"] == "error"
