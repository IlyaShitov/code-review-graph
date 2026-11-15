"""Link pytest consumers to the fixtures they name.

A test asks for a fixture by naming it as a parameter; nothing calls the
fixture, so the parser records no edge. ``callers_of`` on a fixture then comes
back empty, and ``tests_for`` stops at the fixture that builds the data, even
though the walk through test helpers already exists.

pytest picks the fixture by name, closest first: the test's class, the test
module (its own definitions and what it imports), then ``conftest.py`` from the
module's directory up to the repository root. This pass follows that order
over the stored graph after every build and writes one CALLS edge per name,
marked ``resolver: pytest``. It re-creates them each time, so a moved or
renamed fixture never leaves an edge behind; the alternative, resolving in the
parser, goes stale when only a conftest changes, because test modules do not
import their conftest.

Not linked: autouse fixtures (every test in scope uses them implicitly),
fixtures from plugins and the standard ones (``db``, ``request``, ``mocker``),
which have no node, and ``pytest_plugins`` modules. A fixture's own parameters
are resolved from where it is defined, not from each test that requests it.

``fixture_by_name`` edges are a heuristic: a test that takes ``request`` is
linked to every fixture named by a string in its ``parametrize`` values,
whether or not it passes that string to ``request.getfixturevalue``.
"""

from __future__ import annotations

import ast
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .graph import GraphStore
from .parser import is_test_file

RESOLVER = "pytest"
RULE_FIXTURE = "fixture"
# ``request.getfixturevalue(name)`` with the name coming from ``parametrize``.
RULE_BY_NAME = "fixture_by_name"

_FIXTURE_DECORATOR = re.compile(r"^(?:pytest(?:_asyncio)?\.)?fixture\b")
_NOT_FIXTURES = frozenset({"self", "cls"})


@dataclass(frozen=True)
class _Node:
    qn: str
    name: str
    file: str
    parent: str
    params: str
    line: int
    kind: str
    decorators: tuple[ast.expr, ...]


@dataclass(frozen=True)
class _Binding:
    """A fixture bound to a module attribute; ``explicit`` is ``fixture(name=...)``."""

    qn: str
    explicit: str | None

    def fixture_name(self, attribute: str) -> str:
        return self.explicit or attribute


def _dotted(expr: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return ""


def _call_name(expr: ast.expr) -> str:
    return _dotted(expr.func if isinstance(expr, ast.Call) else expr)


def _strings(expr: ast.expr | None) -> list[str]:
    if expr is None:
        return []
    return [
        node.value
        for node in ast.walk(expr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _parse_decorators(extra: str | None) -> tuple[ast.expr, ...]:
    if not extra or "decorators" not in extra:
        return ()
    try:
        texts = json.loads(extra).get("decorators") or []
    except (TypeError, ValueError):
        return ()
    parsed = []
    for text in texts:
        try:
            parsed.append(ast.parse(text.strip(), mode="eval").body)
        except (SyntaxError, ValueError):
            continue
    return tuple(parsed)


def _explicit_fixture_name(decorator: ast.expr) -> str | None:
    if isinstance(decorator, ast.Call):
        for keyword in decorator.keywords:
            if (
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return keyword.value.value
    return None


def _fixture_decorator(node: _Node) -> ast.expr | None:
    for decorator in node.decorators:
        if _FIXTURE_DECORATOR.match(_call_name(decorator)):
            return decorator
    return None


def _usefixtures(decorators: tuple[ast.expr, ...]) -> list[str]:
    names: list[str] = []
    for decorator in decorators:
        if isinstance(decorator, ast.Call) and _call_name(decorator).endswith(
            "mark.usefixtures"
        ):
            for arg in decorator.args:
                names += _strings(arg)
    return names


def _injected_mocks(decorators: tuple[ast.expr, ...]) -> int:
    """How many leading parameters ``mock.patch`` decorators fill with mocks.

    A patch given its replacement (``new=`` or the positional one) injects
    nothing.
    """
    count = 0
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        name = _call_name(decorator)
        if name == "patch" or name.endswith(".patch"):
            replacement_at = 1
        elif name == "patch.object" or name.endswith(".patch.object"):
            replacement_at = 2
        else:
            continue
        if len(decorator.args) <= replacement_at and not any(
            keyword.arg == "new" for keyword in decorator.keywords
        ):
            count += 1
    return count


def _parametrize(decorators: tuple[ast.expr, ...]) -> tuple[set[str], list[str]]:
    """Argument names that ``parametrize`` supplies, and the strings in its values.

    An argument listed in ``indirect`` is still a fixture, so it is not counted
    as supplied.
    """
    supplied: set[str] = set()
    values: list[str] = []
    for decorator in decorators:
        if not (
            isinstance(decorator, ast.Call)
            and _call_name(decorator).endswith("mark.parametrize")
            and decorator.args
        ):
            continue
        names = {
            name.strip()
            for text in _strings(decorator.args[0])
            for name in text.split(",")
            if name.strip()
        }
        for keyword in decorator.keywords:
            if keyword.arg != "indirect":
                continue
            if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                names = set()
            else:
                names -= set(_strings(keyword.value))
        supplied |= names
        if len(decorator.args) > 1:
            values += _strings(decorator.args[1])
    return supplied, values


def _parameters(params: str) -> list[tuple[str, int]]:
    """Parameter names without a default, each with its line offset from ``def``."""
    if not params:
        return []
    try:
        tree = ast.parse(f"def f{params}: pass")
    except (SyntaxError, ValueError):
        return []
    func = tree.body[0]
    if not isinstance(func, ast.FunctionDef):
        return []
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    required = positional[: len(positional) - len(args.defaults)]
    required += [
        arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None
    ]
    return [(arg.arg, arg.lineno - 1) for arg in required]


class _Resolver:
    def __init__(self, store: GraphStore, repo_root: Path) -> None:
        self._conn = store._conn
        self._root = Path(repo_root)
        self.files = {
            row[0]
            for row in self._conn.execute(
                "SELECT file_path FROM nodes WHERE kind = 'File' AND language = 'python'"
            )
        }
        self.nodes = [
            _Node(
                qn=row["qualified_name"],
                name=row["name"],
                file=row["file_path"],
                parent=row["parent_name"] or "",
                params=row["params"] or "",
                line=row["line_start"] or 0,
                kind=row["kind"],
                decorators=_parse_decorators(row["extra"]),
            )
            for row in self._conn.execute(
                "SELECT qualified_name, name, file_path, parent_name, params, line_start, "
                "kind, extra FROM nodes "
                "WHERE language = 'python' AND kind IN ('Function', 'Test', 'Class')"
            )
        ]
        self.fixtures: set[str] = set()
        self._module: dict[str, dict[str, _Binding]] = defaultdict(dict)
        self._class: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        for node in self.nodes:
            if node.kind == "Class":
                continue
            decorator = _fixture_decorator(node)
            if decorator is None:
                continue
            self.fixtures.add(node.qn)
            explicit = _explicit_fixture_name(decorator)
            if node.parent:
                self._class[(node.file, node.parent)][explicit or node.name] = node.qn
            else:
                self._module[node.file][node.name] = _Binding(node.qn, explicit)
        self._imports: dict[str, dict[int, list[str]]] | None = None
        self._bindings: dict[str, dict[str, _Binding]] = {}
        self._visible: dict[str, dict[str, str]] = {}
        self._test_files: dict[str, bool] = {}

    def in_test_file(self, file: str) -> bool:
        if file not in self._test_files:
            self._test_files[file] = is_test_file(file, self._root)
        return self._test_files[file]

    def _import_targets(self, file: str) -> dict[int, list[str]]:
        """Imported files by statement line, kept only where a fixture can come from.

        Following every import walks the whole production code, whose import
        cycles (a package ``__init__`` and its submodules) make the walk
        exponential, since results inside a cycle are not cached. A module can
        pass a fixture on only if a file defining one is reachable from it
        through imports, so every other target is dropped up front.
        """
        if self._imports is None:
            edges = [
                (row[0], row[1], row[2])
                for row in self._conn.execute(
                    "SELECT file_path, line, target_qualified FROM edges "
                    "WHERE kind = 'IMPORTS_FROM'"
                )
                if row[2] in self.files
            ]
            importers: dict[str, set[str]] = defaultdict(set)
            for source, _, target in edges:
                importers[target].add(source)
            reaching = set(self._module)
            pending = list(reaching)
            while pending:
                for importer in importers.get(pending.pop(), ()):
                    if importer not in reaching:
                        reaching.add(importer)
                        pending.append(importer)
            self._imports = defaultdict(lambda: defaultdict(list))
            for source, line, target in edges:
                if target in reaching:
                    self._imports[source][line].append(target)
        return self._imports.get(file, {})

    def _bindings_of(self, file: str) -> dict[str, _Binding]:
        """Module attributes holding fixtures: imported first, then the module's own."""
        return self._collect_bindings(file, frozenset())[0]

    def _collect_bindings(
        self, file: str, resolving: frozenset[str],
    ) -> tuple[dict[str, _Binding], bool]:
        """Bindings of *file*, and whether they are complete.

        A star import that loops back to a module still being collected sees
        that module's bindings as empty. Such a result is right only for this
        walk, so it is not cached: caching it would hide the loop's fixtures
        from every later module that imports this one.
        """
        if file in self._bindings:
            return self._bindings[file], True
        if file in resolving:
            return {}, False
        complete = True
        bindings: dict[str, _Binding] = {}
        targets_by_line = self._import_targets(file)
        if targets_by_line:
            try:
                tree = ast.parse(Path(file).read_bytes())
            except (OSError, SyntaxError, ValueError):
                tree = None
            statements = tree.body if tree else []
            inner = resolving | {file}
            for statement in statements:
                if not isinstance(statement, ast.ImportFrom):
                    continue
                for target in targets_by_line.get(statement.lineno, []):
                    exported, exported_complete = self._collect_bindings(target, inner)
                    complete = complete and exported_complete
                    for alias in statement.names:
                        if alias.name == "*":
                            bindings.update(
                                (attr, binding)
                                for attr, binding in exported.items()
                                if not attr.startswith("_")
                            )
                        elif alias.name in exported:
                            bindings[alias.asname or alias.name] = exported[alias.name]
        bindings.update(self._module.get(file, {}))
        if complete:
            self._bindings[file] = bindings
        return bindings, complete

    def _visible_in(self, file: str) -> dict[str, str]:
        if file not in self._visible:
            self._visible[file] = {
                binding.fixture_name(attr): binding.qn
                for attr, binding in self._bindings_of(file).items()
            }
        return self._visible[file]

    def _conftests(self, file: str) -> list[str]:
        found = []
        directory = Path(file).parent
        while directory.is_relative_to(self._root):
            conftest = str(directory / "conftest.py")
            if conftest != file and conftest in self.files:
                found.append(conftest)
            if directory == self._root:
                break
            directory = directory.parent
        return found

    def resolve(self, name: str, node: _Node) -> str | None:
        levels: list[dict[str, str]] = []
        if node.parent:
            levels.append(self._class.get((node.file, node.parent), {}))
        levels.append(self._visible_in(node.file))
        levels += [self._visible_in(conftest) for conftest in self._conftests(node.file)]
        for level in levels:
            qn = level.get(name)
            if qn and qn != node.qn:
                return qn
        return None


def link_pytest_fixtures(store: GraphStore, repo_root: Path) -> dict | None:
    """Write consumer -> fixture CALLS edges for the whole graph; ``None`` without fixtures."""
    started = time.monotonic()
    conn = store._conn
    resolver = _Resolver(store, Path(repo_root))
    conn.execute("SAVEPOINT pytest_fixtures")
    try:
        removed = conn.execute(
            "DELETE FROM edges WHERE kind = 'CALLS' "
            "AND json_extract(extra, '$.resolver') = ?",
            (RESOLVER,),
        ).rowcount
        if not resolver.fixtures:
            return None
        stats = _insert_edges(conn, resolver)
    except Exception:
        conn.execute("ROLLBACK TO pytest_fixtures")
        raise
    finally:
        conn.execute("RELEASE pytest_fixtures")
        store._invalidate_cache()
    return {
        "fixtures": len(resolver.fixtures),
        **stats,
        "removed_previous_edges": removed,
        "seconds": round(time.monotonic() - started, 1),
    }


def _insert_edges(conn, resolver: _Resolver) -> dict:
    class_marks = {
        (node.file, node.name): _usefixtures(node.decorators)
        for node in resolver.nodes
        if node.kind == "Class" and node.decorators
    }
    edges: dict[tuple[str, str, int], str] = {}
    consumers = 0
    unresolved = 0
    for node in resolver.nodes:
        if node.kind == "Class":
            continue
        # The parser marks any ``test_*`` function a test; outside a test file
        # it is production code pytest never collects.
        is_test = node.kind == "Test" and resolver.in_test_file(node.file)
        if not (is_test or node.qn in resolver.fixtures):
            continue
        consumers += 1
        supplied, values = _parametrize(node.decorators)
        parameters = [
            (name, offset)
            for name, offset in _parameters(node.params)
            if name not in _NOT_FIXTURES
        ][_injected_mocks(node.decorators):]
        requested = [
            (name, node.line + offset)
            for name, offset in parameters
            if name not in supplied
        ]
        marked = _usefixtures(node.decorators) + class_marks.get((node.file, node.parent), [])
        requested += [(name, node.line) for name in marked]
        names = {name for name, _ in requested}
        for name, line in requested:
            target = resolver.resolve(name, node)
            if target is None:
                unresolved += 1
            else:
                edges.setdefault((node.qn, target, line), RULE_FIXTURE)
        if "request" in names:
            for value in values:
                target = resolver.resolve(value, node)
                if target is not None:
                    edges.setdefault((node.qn, target, node.line), RULE_BY_NAME)

    file_of = {node.qn: node.file for node in resolver.nodes}
    now = time.time()
    conn.executemany(
        "INSERT INTO edges(kind, source_qualified, target_qualified, file_path, line, extra, "
        "confidence, confidence_tier, updated_at, target_resolution) "
        "VALUES ('CALLS', ?, ?, ?, ?, ?, 1.0, 'INFERRED', ?, 'direct')",
        [
            (
                source,
                target,
                file_of[source],
                line,
                json.dumps({"resolver": RESOLVER, "rule": rule}),
                now,
            )
            for (source, target, line), rule in edges.items()
        ],
    )
    return {
        "consumers": consumers,
        "edges": len(edges),
        "by_name": sum(rule == RULE_BY_NAME for rule in edges.values()),
        "unresolved": unresolved,
    }
