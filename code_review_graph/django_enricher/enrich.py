"""Обогащение графа code-review-graph Django-проекта: вызовы методов менеджеров и кверисетов, MRO,
фильтры прав, константы и лейблы — прямо в `.code-review-graph/graph.db`."""

import ast
import sqlite3
import time
from pathlib import Path

from code_review_graph.graph import GraphStore

from .index import Index
from .inject import apply
from .resolve import class_docstrings, constant_nodes, resolve_project


def find_source_root(repo: Path) -> Path:
    """
    Корень импортов — каталог `manage.py`, ближайшего к корню репозитория; без него — сам
    репозиторий.
    """
    candidates = [
        path
        for path in [repo / "manage.py", *repo.glob("*/manage.py"), *repo.glob("*/*/manage.py")]
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(repo).parts)
    ]
    return min(candidates, key=lambda path: len(path.parts)).parent if candidates else repo


def find_settings_module(source_root: Path) -> str:
    """
    Модуль настроек из `os.environ.setdefault('DJANGO_SETTINGS_MODULE', '...')` в `manage.py`;
    не нашёлся — пустая строка.
    """
    try:
        tree = ast.parse((source_root / "manage.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return ""
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
        ):
            continue
        values = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
        if (
            len(values) == 2
            and values[0] == "DJANGO_SETTINGS_MODULE"
            and isinstance(values[1], str)
        ):
            return values[1]
    return ""


def find_site_packages(repo: Path) -> Path | None:
    for venv in (".venv", "venv"):
        found = sorted(repo.glob(f"{venv}/lib/python3*/site-packages"))
        if found:
            return found[0]
    return None


def project_files(con: sqlite3.Connection, source_root: Path) -> list[Path]:
    """
    Python-файлы ровно из графа (он индексирует `git ls-files`), под корнем импортов, без
    миграций.
    """
    rows = con.execute("select file_path from nodes where kind = 'File' and language = 'python'")
    paths = [Path(row[0]) for row in rows]
    return [
        path
        for path in paths
        if path.is_relative_to(source_root) and "migrations" not in path.parts
    ]


def is_enriched(store: GraphStore) -> bool:
    """
    Есть ли в графе рёбра резолвера. Граф, собранный upstream, их не содержит — его нужно
    обогатить и при сборке без изменений.
    """
    return (
        store._conn.execute(
            "select 1 from edges where json_extract(extra, '$.resolver') = 'django' limit 1"
        ).fetchone()
        is not None
    )


def enrich_django(store: GraphStore, repo: Path) -> dict | None:
    """Enrich the current build using its connection and repository write lock.

    Project modules are parsed as AST, never imported. The caller runs FTS
    and other postprocessing afterwards, just as for the other resolvers.
    """
    source_root = find_source_root(repo)
    if not (source_root / "manage.py").is_file():
        return None
    started = time.monotonic()
    site_packages = find_site_packages(repo)
    con = store._conn
    index = Index(
        source_root,
        project_files(con, source_root),
        site_packages,
        settings_module=find_settings_module(source_root),
    )
    results, resolve_stats = resolve_project(index)
    con.execute("SAVEPOINT django_enrichment")
    try:
        inject_stats, _ = apply(con, results, constant_nodes(index), class_docstrings(index))
    except Exception:
        con.execute("ROLLBACK TO django_enrichment")
        raise
    finally:
        con.execute("RELEASE django_enrichment")
    store._invalidate_cache()
    return {
        "source_root": str(source_root),
        "site_packages": str(site_packages or ""),
        "lib_count": len(
            {
                module.name.split(".")[0]
                for module in index.modules.values()
                if not module.is_project
            }
        ),
        "unparsed_files": len(index.errors),
        "errors": index.errors,
        "resolve": resolve_stats,
        "inject": inject_stats,
        "seconds": round(time.monotonic() - started, 1),
    }
