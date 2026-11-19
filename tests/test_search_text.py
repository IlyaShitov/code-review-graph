"""search_text finds text the graph does not index and names the node around each match."""

from textwrap import dedent

import pytest

from code_review_graph.compact import format_compact
from code_review_graph.tools.build import build_or_update_graph
from code_review_graph.tools.query import search_text

TRACKED = ["app.py", "ci.yml", "blob.bin", "paged.py"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A literal at module level and inside a method, a YAML file the graph does not
    break into symbols, a binary file, a file with a form feed, and a tracked file deleted
    from the working tree."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "app.py").write_text(
        dedent("""\
            TITLE = "Транспортная работа"


            class Report:
                def name(self):
                    return "Транспортная работа за день"
        """),
        encoding="utf-8",
    )
    (tmp_path / "ci.yml").write_text("stage: транспортная работа\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\0" + "Транспортная работа".encode())
    (tmp_path / "paged.py").write_text("\f\ndef paged():\n    return 'marker'\n")
    monkeypatch.setattr(
        "code_review_graph.incremental.get_all_tracked_files", lambda *args, **kwargs: TRACKED
    )
    monkeypatch.setattr(
        "code_review_graph.tools.query.get_all_tracked_files",
        lambda *args, **kwargs: [*TRACKED, "gone.py"],
    )
    build_or_update_graph(repo_root=str(tmp_path), full_rebuild=True, postprocess="none")
    return tmp_path


def _found(result):
    return [(row["name"], row["kind"], row["line"]) for row in result["results"]]


def test_literal_matches_name_the_enclosing_node(repo):
    result = search_text("транспортная работа", repo_root=str(repo))

    assert _found(result) == [
        ("app.py", "File", 1),
        ("name", "Function", 6),
        ("ci.yml", "File", 1),
    ]
    assert result["results"][1]["qualified_name"].endswith("app.py::Report.name")
    assert result["results"][1]["text"] == 'return "Транспортная работа за день"'
    assert result["total"] == 3
    assert result["truncated"] is False


def test_case_regex_and_path_narrow_the_matches(repo):
    assert _found(search_text("транспортная", case_sensitive=True, repo_root=str(repo))) == [
        ("ci.yml", "File", 1),
    ]
    assert _found(search_text(r"работа за \w+", regex=True, repo_root=str(repo))) == [
        ("name", "Function", 6),
    ]
    assert _found(search_text("работа", path="*.yml", repo_root=str(repo))) == [
        ("ci.yml", "File", 1),
    ]


def test_limit_keeps_the_total(repo):
    result = search_text("работа", limit=1, repo_root=str(repo))

    assert len(result["results"]) == 1
    assert result["total"] == 3
    assert result["truncated"] is True
    assert result["results_omitted"] == 2


@pytest.mark.parametrize("query", ["", "x" * 201])
def test_query_length_is_bounded(repo, query):
    assert search_text(query, repo_root=str(repo))["status"] == "error"


def test_invalid_regex_is_an_error(repo):
    result = search_text("(", regex=True, repo_root=str(repo))

    assert result["status"] == "error"
    assert "Invalid regular expression" in result["error"]


def test_compact_output_shows_node_line_and_text(repo):
    compact = format_compact(search_text("за день", repo_root=str(repo)), str(repo))

    assert "app.py::Report.name  (Function 5-6, line: 6, text: return" in compact


def test_form_feed_does_not_shift_line_numbers(repo):
    """Lines are counted on "\\n" only, as the graph counts them, so a form feed
    before a function leaves the match inside that function."""
    assert _found(search_text("marker", repo_root=str(repo))) == [("paged", "Function", 3)]
