"""Django enrichment belongs to the build, including custom database paths."""

from textwrap import dedent

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.tools.build import _run_postprocess, build_or_update_graph


@pytest.fixture
def django_repo(tmp_path, monkeypatch):
    (tmp_path / "manage.py").write_text("")
    (tmp_path / "models.py").write_text(
        dedent("""\
        from django.db import models

        MAX_ITEMS = 5

        class ItemQuerySet(models.QuerySet):
            def active(self):
                return self

        class Item(models.Model):
            objects = ItemQuerySet.as_manager()

        def report():
            return Item.objects.active(), MAX_ITEMS
    """)
    )
    monkeypatch.setattr(
        "code_review_graph.incremental.get_all_tracked_files",
        lambda *args, **kwargs: ["manage.py", "models.py"],
    )
    return tmp_path


def test_full_build_enriches_calls_constants_and_search(django_repo):
    with GraphStore(django_repo / "custom.db") as store:
        result = full_build(django_repo, store)
        assert result["errors"] == []
        assert [
            row[0] for row in store._conn.execute("SELECT name FROM nodes WHERE kind = 'Constant'")
        ] == ["MAX_ITEMS"]
        assert (
            store._conn.execute(
                "SELECT target_qualified FROM edges WHERE kind = 'CALLS' "
                "AND source_qualified = ? AND target_qualified = ?",
                (
                    f"{django_repo}/models.py::report",
                    f"{django_repo}/models.py::ItemQuerySet.active",
                ),
            ).fetchone()
            is not None
        )
        _run_postprocess(store, result, "minimal", full_rebuild=True)
        assert [
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'MAX_ITEMS'"
            )
        ] == ["MAX_ITEMS"]


def test_incremental_update_restores_enrichment_and_fts(django_repo):
    with GraphStore(django_repo / "custom.db") as store:
        full_build(django_repo, store)
        path = django_repo / "models.py"
        path.write_text(path.read_text().replace("MAX_ITEMS", "ITEM_LIMIT"))
        for _ in range(2):
            result = incremental_update(django_repo, store, changed_files=["models.py"])
            _run_postprocess(store, result, "minimal", changed_files=["models.py"])
            assert [
                row[0]
                for row in store._conn.execute("SELECT name FROM nodes WHERE kind = 'Constant'")
            ] == ["ITEM_LIMIT"]
            assert [
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'ITEM_LIMIT'"
                )
            ] == ["ITEM_LIMIT"]
            assert (
                store._conn.execute(
                    "SELECT count(*) FROM edges WHERE kind = 'REFERENCES' AND target_qualified = ?",
                    (f"{django_repo}/models.py::ITEM_LIMIT",),
                ).fetchone()[0]
                == 1
            )


def test_non_django_project_is_skipped(django_repo):
    (django_repo / "manage.py").unlink()
    with GraphStore(django_repo / "custom.db") as store:
        full_build(django_repo, store)
        assert (
            store._conn.execute("SELECT count(*) FROM nodes WHERE kind = 'Constant'").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("postprocess", ["none", "minimal", "full"])
def test_build_entrypoint_enriches_before_postprocessing(django_repo, postprocess):
    (django_repo / ".git").mkdir()
    result = build_or_update_graph(
        repo_root=str(django_repo),
        full_rebuild=True,
        postprocess=postprocess,
    )
    assert result["status"] == "ok"
    assert result["django_enrichment"]["inject"]["constants"] == 1
    assert result["django_enrichment"]["unparsed_files"] == 0


def test_noop_update_enriches_existing_graph(django_repo, monkeypatch):
    # An existing graph may have been built by the upstream version.
    with GraphStore(django_repo / "custom.db") as store:
        with monkeypatch.context() as patch:
            patch.setattr("code_review_graph.incremental.enrich_django", lambda *args: None)
            result = full_build(django_repo, store)
            _run_postprocess(store, result, "full", full_rebuild=True)
        result = incremental_update(django_repo, store, changed_files=[])
        assert result["files_updated"] == 0
        assert result["django_enrichment"]["inject"]["constants"] == 1
        _run_postprocess(store, result, "full", changed_files=[])
        assert store._conn.execute(
            "SELECT community_id FROM nodes WHERE kind = 'Constant'"
        ).fetchone()[0] is not None


def test_enrichment_write_failure_rolls_back(django_repo, monkeypatch):
    from code_review_graph.django_enricher.enrich import enrich_django

    def fail_after_delete(con, *args):
        con.execute("DELETE FROM nodes")
        raise RuntimeError("injection failed")

    with GraphStore(django_repo / "custom.db") as store:
        full_build(django_repo, store)
        nodes_before = [tuple(row) for row in store._conn.execute("SELECT * FROM nodes")]
        monkeypatch.setattr("code_review_graph.django_enricher.enrich.apply", fail_after_delete)
        with pytest.raises(RuntimeError, match="injection failed"):
            enrich_django(store, django_repo)
        assert [tuple(row) for row in store._conn.execute("SELECT * FROM nodes")] == nodes_before


def test_noop_update_preserves_constant_identity_and_community(django_repo):
    with GraphStore(django_repo / "custom.db") as store:
        result = full_build(django_repo, store)
        _run_postprocess(store, result, "full", full_rebuild=True)
        query = "SELECT id, community_id FROM nodes WHERE kind = 'Constant'"
        before = tuple(store._conn.execute(query).fetchone())
        assert before[1] is not None
        result = incremental_update(django_repo, store, changed_files=[])
        assert tuple(store._conn.execute(query).fetchone()) == before
        _run_postprocess(store, result, "full", changed_files=[])
        after = tuple(store._conn.execute(query).fetchone())
        assert after[0] == before[0]
        assert after[1] is not None
