import sqlite3
from pathlib import Path

import pytest

from code_review_graph.django_enricher.enrich import find_source_root, project_files
from code_review_graph.django_enricher.index import Index


# модули библиотек догружаются в индекс во время разрешения
@pytest.mark.usefixtures("resolved")
def test_discovery(workspace: Path, project_graph: sqlite3.Connection, index: Index) -> None:
    """Корень импортов — каталог manage.py; из графа берутся файлы под ним без миграций;
    из site-packages догружаются только модули, на которые сослался проект, и не Django."""
    root = find_source_root(workspace)
    files = {path.relative_to(root).as_posix() for path in project_files(project_graph, root)}
    lib_modules = {module.name for module in index.modules.values() if not module.is_project}

    assert root == workspace / "proj"
    assert files == {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "migrations" not in path.parts
    }
    assert lib_modules == {"libpkg.mixins"}
