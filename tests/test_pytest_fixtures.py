"""A pytest fixture is used by naming it, so the build links each consumer to it."""

from textwrap import dedent

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update

FILES = {
    "conftest.py": """\
        import pytest

        from pkg.tests.conftest import *


        @pytest.fixture
        def root_only():
            return 1


        @pytest.fixture
        def shadowed():
            return "root"
    """,
    "pkg/__init__.py": "",
    "pkg/service.py": """\
        def make_item():
            return {}
    """,
    "pkg/tests/__init__.py": "",
    "pkg/tests/conftest.py": """\
        import pytest

        from .fixtures.items import *


        @pytest.fixture
        def shadowed():
            return "pkg"
    """,
    "pkg/tests/fixtures/__init__.py": "",
    "pkg/tests/fixtures/items.py": """\
        import pytest

        from pkg.service import make_item


        @pytest.fixture
        def item():
            return make_item()


        @pytest.fixture
        def item_list(item):
            return [item]


        @pytest.fixture(name="renamed")
        def renamed_impl():
            return 2
    """,
    "support/__init__.py": "",
    "support/fx.py": """\
        import pytest


        @pytest.fixture
        def named_a():
            return 1


        @pytest.fixture
        def named_b():
            return 2
    """,
    "pkg/tests/test_items.py": """\
        import pytest

        from support.fx import named_a


        @pytest.fixture
        def local():
            return 3


        def test_a(item, shadowed, local, db, request):
            pass


        @pytest.mark.usefixtures("root_only")
        def test_b():
            pass


        @pytest.mark.parametrize("item", [1])
        def test_param(item):
            pass


        @pytest.mark.parametrize("fixture_name", ["item", "nope"])
        def test_by_name(fixture_name, request):
            request.getfixturevalue(fixture_name)


        def test_renamed(renamed, named_a, named_b):
            pass


        class TestGroup:
            @pytest.fixture
            def item(self, item):
                return item

            def test_m(self, item):
                pass


        @pytest.mark.usefixtures("local")
        class TestMarked:
            def test_x(self):
                pass


        from unittest import mock


        @mock.patch("pkg.service.make_item")
        def test_patched(item, local):
            pass
    """,
    "pkg/health.py": """\
        def test_connection(shadowed):
            return shadowed
    """,
    "other/__init__.py": "",
    "other/tests/__init__.py": "",
    "other/tests/test_other.py": """\
        def test_uses_global(item_list, shadowed):
            pass
    """,
    "other/tests/test_nested.py": """\
        import pytest


        class TestOuter:
            @pytest.mark.usefixtures("root_only")
            class TestInner:
                def test_x(self):
                    pass
    """,
    "other/tests/cycle_a.py": """\
        import pytest

        from other.tests.cycle_b import *


        @pytest.fixture
        def fx_a():
            return 1
    """,
    "other/tests/cycle_b.py": """\
        import pytest

        from other.tests.cycle_a import *


        @pytest.fixture
        def fx_b():
            return 2
    """,
    "other/tests/test_cycle_a.py": """\
        from other.tests.cycle_a import *


        def test_sees_b(fx_b):
            pass
    """,
    "other/tests/test_cycle_b.py": """\
        from other.tests.cycle_b import *


        def test_sees_a(fx_a):
            pass
    """,
}


@pytest.fixture
def pytest_repo(tmp_path, monkeypatch):
    for name, text in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text))
    monkeypatch.setattr(
        "code_review_graph.incremental.get_all_tracked_files",
        lambda *args, **kwargs: list(FILES),
    )
    return tmp_path


def _fixture_edges(store, repo):
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, line, "
        "json_extract(extra, '$.rule') FROM edges "
        "WHERE kind = 'CALLS' AND json_extract(extra, '$.resolver') = 'pytest'"
    ).fetchall()
    prefix = f"{repo}/"
    return {
        (source.removeprefix(prefix), target.removeprefix(prefix), line, rule)
        for source, target, line, rule in rows
    }


ITEMS = "pkg/tests/fixtures/items.py"
TESTS = "pkg/tests/test_items.py"

EXPECTED = {
    (f"{ITEMS}::item_list", f"{ITEMS}::item", 12, "fixture"),
    (f"{TESTS}::test_a", f"{ITEMS}::item", 11, "fixture"),
    (f"{TESTS}::test_a", "pkg/tests/conftest.py::shadowed", 11, "fixture"),
    (f"{TESTS}::test_a", f"{TESTS}::local", 11, "fixture"),
    (f"{TESTS}::test_b", "conftest.py::root_only", 16, "fixture"),
    (f"{TESTS}::test_by_name", f"{ITEMS}::item", 26, "fixture_by_name"),
    (f"{TESTS}::test_renamed", f"{ITEMS}::renamed_impl", 30, "fixture"),
    (f"{TESTS}::test_renamed", "support/fx.py::named_a", 30, "fixture"),
    (f"{TESTS}::TestGroup.item", f"{ITEMS}::item", 36, "fixture"),
    (f"{TESTS}::TestGroup.test_m", f"{TESTS}::TestGroup.item", 39, "fixture"),
    (f"{TESTS}::TestMarked.test_x", f"{TESTS}::local", 45, "fixture"),
    ("other/tests/test_other.py::test_uses_global", f"{ITEMS}::item_list", 1, "fixture"),
    ("other/tests/test_other.py::test_uses_global", "conftest.py::shadowed", 1, "fixture"),
    # The mock from `patch` takes the first parameter, so `item` is no fixture here.
    (f"{TESTS}::test_patched", f"{TESTS}::local", 53, "fixture"),
    # A mark on a nested class reaches its tests, keyed by the bare class name
    # their parent carries.
    ("other/tests/test_nested.py::TestInner.test_x", "conftest.py::root_only", 7, "fixture"),
    # Star imports that loop back still carry each module's fixtures to the other.
    ("other/tests/test_cycle_a.py::test_sees_b", "other/tests/cycle_b.py::fx_b", 4, "fixture"),
    ("other/tests/test_cycle_b.py::test_sees_a", "other/tests/cycle_a.py::fx_a", 4, "fixture"),
}


def test_full_build_links_consumers_to_the_fixture_pytest_would_pick(pytest_repo):
    with GraphStore(pytest_repo / "graph.db") as store:
        result = full_build(pytest_repo, store)
        assert result["errors"] == []
        # `pkg/health.py::test_connection` is named like a test but is no test
        # code, so its `shadowed` parameter links nowhere.
        assert _fixture_edges(store, pytest_repo) == EXPECTED
        stats = result["pytest_fixtures"]
        assert stats["edges"] == len(EXPECTED)
        # `db`, `request` twice, and `named_b`, which the test module never
        # imports. Parametrize strings that name no fixture are plain values.
        assert stats["unresolved"] == 4


def test_fixture_route_reaches_tests_of_production_code(pytest_repo):
    with GraphStore(pytest_repo / "graph.db") as store:
        full_build(pytest_repo, store)
        tests = {
            row["qualified_name"].removeprefix(f"{pytest_repo}/")
            for row in store.get_transitive_tests(f"{pytest_repo}/pkg/service.py::make_item")
        }
        assert {f"{TESTS}::test_a", f"{TESTS}::test_by_name"} <= tests


def test_rebuild_replaces_edges_without_duplicates(pytest_repo):
    with GraphStore(pytest_repo / "graph.db") as store:
        full_build(pytest_repo, store)
        result = incremental_update(pytest_repo, store, changed_files=[])
        assert result["pytest_fixtures"]["edges"] == len(EXPECTED)
        assert _fixture_edges(store, pytest_repo) == EXPECTED

        path = pytest_repo / ITEMS
        path.write_text(path.read_text().replace("def item(", "def thing("))
        incremental_update(pytest_repo, store, changed_files=[ITEMS])
        edges = _fixture_edges(store, pytest_repo)
        assert not {edge for edge in edges if edge[1] == f"{ITEMS}::item"}
        assert (f"{TESTS}::test_a", "pkg/tests/conftest.py::shadowed", 11, "fixture") in edges


def test_project_without_fixtures_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n")
    monkeypatch.setattr(
        "code_review_graph.incremental.get_all_tracked_files",
        lambda *args, **kwargs: ["app.py"],
    )
    with GraphStore(tmp_path / "graph.db") as store:
        assert full_build(tmp_path, store)["pytest_fixtures"] is None
