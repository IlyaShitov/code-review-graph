"""
Вывод типов получателей вызовов, ссылки на константы и лейблы классов — всё, что дописывается в
граф.
"""

import ast
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from .index import (
    QS_PRESERVING,
    SELF_ANNOTATIONS,
    ClassInfo,
    ConstantInfo,
    Index,
    ModuleInfo,
    walk_local,
)

RULE_CHAIN: Final[str] = "queryset_chain"
RULE_MRO: Final[str] = "mro"
RULE_PERMISSION: Final[str] = "permission_filter"
RULE_CONSTANT: Final[str] = "constant_ref"
# правила, по которым рёбра вставляются заново, а не переписываются из голых рёбер графа
INSERTED_RULES: Final[frozenset[str]] = frozenset({RULE_PERMISSION, RULE_CONSTANT})


@dataclass(frozen=True)
class Instance:
    cls: ClassInfo
    after: str = ""  # для super(): искать метод после этого класса в MRO


@dataclass(frozen=True)
class ClassRef:
    cls: ClassInfo


@dataclass(frozen=True)
class Resolved:
    file: str
    line: int
    name: str  # вызываемый метод или константа
    target: str
    rule: str
    scope: str
    via: str = ""


@dataclass(frozen=True)
class ConstantNode:
    qn: str
    name: str
    file: str
    line_start: int
    line_end: int
    signature: str
    docstring: str


def _bound_names(target: ast.AST) -> set[str]:
    """Имена, которые цель присваивания связывает: `a`, `a, b`, но не `i` в `stats[i] += 1`."""
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }


def constant_refs(
    index: Index,
    module: ModuleInfo,
    scope_node: ast.AST,
    local_names: set[str],
    scope: str,
) -> list[Resolved]:
    """
    Ссылки на константы проекта в области: имя своего модуля, импортированное имя,
    `module.CONST`.
    """
    out = []
    for node in walk_local(scope_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            shadowed = node.id in local_names
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            shadowed = node.value.id in local_names
        else:
            continue
        target = None if shadowed else index.resolve_expr(module, node)
        if isinstance(target, ConstantInfo):
            out.append(
                Resolved(
                    str(module.path), node.lineno, target.name, target.qn, RULE_CONSTANT, scope
                )
            )
    return out


class FunctionResolver:
    def __init__(
        self,
        index: Index,
        module: ModuleInfo,
        owner: ClassInfo | None,
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: str,
    ):
        self.index = index
        self.module = module
        self.owner = owner
        self.fn = fn
        self.scope = scope
        self.env: dict[str, Instance] = {}
        self.fixed: set[str] = set()
        self.local_names: set[str] = set()
        self._init_params()
        self._init_assignments()

    def _ann_instance(self, annotation: ast.expr | None) -> Instance | None:
        if annotation is None:
            return None
        if ast.unparse(annotation).strip("'\"") in SELF_ANNOTATIONS and self.owner is not None:
            return Instance(self.owner)
        resolved = self.index.resolve_expr(self.module, annotation)
        return Instance(resolved) if isinstance(resolved, ClassInfo) else None

    def _init_params(self) -> None:
        args = self.fn.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        self.local_names |= {p.arg for p in params} | {
            a.arg for a in (args.vararg, args.kwarg) if a
        }
        decorators = {ast.unparse(d) for d in self.fn.decorator_list}
        if self.owner is not None and params and not decorators & {"staticmethod", "classmethod"}:
            first, params = params[0], params[1:]
            self.env[first.arg] = Instance(self.owner)
            self.fixed.add(first.arg)
        for param in params:
            instance = self._ann_instance(param.annotation)
            if instance is not None:
                self.env[param.arg] = instance
                self.fixed.add(param.arg)

    def _init_assignments(self) -> None:
        assigned: dict[str, list[ast.expr]] = defaultdict(list)
        blocked: set[str] = set()
        for node in walk_local(self.fn):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                self.local_names.add(name)
                instance = self._ann_instance(node.annotation)
                if instance is not None and name not in self.fixed:
                    self.env[name] = instance
                    self.fixed.add(name)
            elif isinstance(node, ast.Assign):
                if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    assigned[node.targets[0].id].append(node.value)
                else:
                    for target in node.targets:
                        blocked |= _bound_names(target)
            elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
                blocked |= _bound_names(node.target)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        blocked |= _bound_names(item.optional_vars)
            elif isinstance(node, ast.NamedExpr):
                blocked.add(node.target.id)
        self.local_names |= set(assigned) | blocked
        for _ in range(2):
            for name, values in assigned.items():
                if name in self.fixed or name in blocked:
                    continue
                # тип выводим по присваиваниям, не ссылающимся на саму переменную, и сверяем со
                # всеми
                independent = [
                    value
                    for value in values
                    if not any(
                        isinstance(node, ast.Name) and node.id == name for node in ast.walk(value)
                    )
                ]
                types = {self.infer(value) for value in independent}
                if len(types) != 1 or None in types:
                    self.env.pop(name, None)
                    continue
                (candidate,) = types
                self.env[name] = candidate
                if any(self.infer(value) != candidate for value in values):
                    self.env.pop(name, None)

    # --- вывод типа выражения --------------------------------------------------------------------

    def infer(self, expr: ast.expr) -> Instance | ClassRef | None:
        if isinstance(expr, ast.Name):
            if expr.id in self.env:
                return self.env[expr.id]
            if expr.id in self.local_names:
                return None
            resolved = self.index.resolve_expr(self.module, expr)
            return ClassRef(resolved) if isinstance(resolved, ClassInfo) else None
        if isinstance(expr, ast.Attribute):
            base = self.infer(expr.value)
            if isinstance(base, ClassRef):
                queryset = self.index.managers(base.cls).get(expr.attr)
                return Instance(queryset) if queryset is not None else None
            if isinstance(base, Instance):
                found = self.index.field_ann(base.cls, expr.attr)
                if found is None:
                    return None
                owner, annotation = found
                resolved = self.index.resolve_expr(self.index.modules[owner.module], annotation)
                return Instance(resolved) if isinstance(resolved, ClassInfo) else None
            if isinstance(expr.value, (ast.Name, ast.Attribute)):
                resolved = self.index.resolve_expr(self.module, expr)
                return ClassRef(resolved) if isinstance(resolved, ClassInfo) else None
            return None
        if isinstance(expr, ast.Call):
            func = expr.func
            if (
                isinstance(func, ast.Name)
                and func.id == "super"
                and not expr.args
                and self.owner is not None
            ):
                return Instance(self.owner, after=self.owner.qn)
            if isinstance(func, ast.Attribute):
                receiver = self.infer(func.value)
                if isinstance(receiver, Instance):
                    return self.call_result(receiver, func.attr)
                if receiver is not None:
                    return None
            callee = self.infer(func)
            return Instance(callee.cls) if isinstance(callee, ClassRef) else None
        return None

    def call_result(self, receiver: Instance, name: str) -> Instance | None:
        if name in QS_PRESERVING and self.index.is_queryset(receiver.cls):
            return Instance(receiver.cls)
        found = self.index.find_method(receiver.cls, name, after=receiver.after)
        if found is None:
            return None
        owner, method = found
        if method.returns_self:
            return Instance(receiver.cls)
        resolved = self.index.resolve_expr(self.index.modules[owner.module], method.node.returns)
        return Instance(resolved) if isinstance(resolved, ClassInfo) else None

    # --- рёбра ------------------------------------------------------------------------------------

    def run(self) -> list[Resolved]:
        out: list[Resolved] = []
        for node in walk_local(self.fn):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                receiver = self.infer(node.func.value)
                if isinstance(receiver, Instance):
                    out.extend(self._method_edge(node, receiver))
            out.extend(self._permission_edges(node))
        out.extend(constant_refs(self.index, self.module, self.fn, self.local_names, self.scope))
        return out

    def _method_edge(self, call: ast.Call, receiver: Instance) -> list[Resolved]:
        name = call.func.attr
        is_queryset = self.index.is_queryset(receiver.cls)
        if name in QS_PRESERVING and is_queryset:
            return []
        found = self.index.find_method(receiver.cls, name, after=receiver.after)
        if found is None or not found[0].is_project:
            return []
        rule = RULE_CHAIN if is_queryset else RULE_MRO
        return [
            Resolved(
                str(self.module.path), call.lineno, name, f"{found[0].qn}.{name}", rule, self.scope
            )
        ]

    def _permission_edges(self, call: ast.Call) -> list[Resolved]:
        if not isinstance(call.func, (ast.Name, ast.Attribute)):
            return []
        callee = self.infer(call.func)
        if not isinstance(callee, ClassRef):
            return []
        chains = self.index.queryset_chains(callee.cls)
        if not chains:
            return []
        args = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        for name, value in zip(self.index.field_order(callee.cls), call.args):
            args.setdefault(name, value)
        if "queryset" not in args:
            return []
        start = self.infer(args["queryset"])
        if not isinstance(start, Instance):
            return []
        out = []
        for chain in chains:
            current: Instance | None = start
            for name in chain:
                if current is None:
                    break
                if not (name in QS_PRESERVING and self.index.is_queryset(current.cls)):
                    found = self.index.find_method(current.cls, name)
                    if found is not None and found[0].is_project:
                        out.append(
                            Resolved(
                                str(self.module.path),
                                call.lineno,
                                name,
                                f"{found[0].qn}.{name}",
                                RULE_PERMISSION,
                                self.scope,
                                via=callee.cls.name,
                            )
                        )
                current = self.call_result(current, name)
        return out


Scope = tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, ClassInfo | None, str]


def iter_scopes(module: ModuleInfo) -> Iterator[Scope]:
    """Функции и классы модуля с владельцем-классом (для методов) и квалифицированным именем."""

    def visit(node: ast.AST, owner: ClassInfo | None, prefix: str) -> Iterator[Scope]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                cls = module.classes.get(child.name) if node is module.tree else None
                yield child, None, f"{prefix}{child.name}"
                yield from visit(child, cls, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield (
                    child,
                    owner if isinstance(node, ast.ClassDef) else None,
                    f"{prefix}{child.name}",
                )
                yield from visit(child, None, f"{prefix}{child.name}.")
            else:
                yield from visit(child, owner, prefix)

    yield from visit(module.tree, None, "")


def _filter_ambiguous(
    items: list[Resolved], calls_per_line: Counter, stats: Counter
) -> list[Resolved]:
    """Строка+метод переписываются, только если все такие вызовы в строке разрешены в одну цель."""
    groups: dict[tuple[int, str], list[Resolved]] = defaultdict(list)
    for item in items:
        if item.rule not in INSERTED_RULES:
            groups[(item.line, item.name)].append(item)
    kept = []
    for key, group in groups.items():
        if len({item.target for item in group}) > 1 or len(group) < calls_per_line[key]:
            stats["ambiguous"] += 1
            continue
        kept.append(group[0])
    kept.extend(dict.fromkeys(item for item in items if item.rule in INSERTED_RULES))
    return kept


def resolve_project(index: Index) -> tuple[list[Resolved], Counter]:
    stats: Counter = Counter()
    results: list[Resolved] = []
    # список заранее: модули библиотек догружаются в индекс по ходу разрешения
    for module in [module for module in index.modules.values() if module.is_project]:
        stats["files"] += 1
        calls_per_line = Counter(
            (node.lineno, node.func.attr)
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        )
        # уровень модуля: глобальные имена не затеняются, константы своего модуля разрешаются
        # напрямую
        module_results = constant_refs(index, module, module.tree, set(), "")
        for node, owner, scope in iter_scopes(module):
            if isinstance(node, ast.ClassDef):
                # атрибуты класса затеняют одноимённые константы модуля в теле класса
                class_names: set[str] = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        class_names.update(*(_bound_names(target) for target in stmt.targets))
                    elif isinstance(stmt, ast.AnnAssign):
                        class_names |= _bound_names(stmt.target)
                module_results.extend(constant_refs(index, module, node, class_names, scope))
                continue
            stats["functions"] += 1
            module_results.extend(FunctionResolver(index, module, owner, node, scope).run())
        results.extend(_filter_ambiguous(module_results, calls_per_line, stats))
    for item in results:
        stats[f"rule:{item.rule}"] += 1
    return results, stats


def constant_nodes(index: Index) -> list[ConstantNode]:
    """
    Узлы констант: в `docstring` — строковое значение или строковые keyword-аргументы
    (`verbose_name`, `codename`).
    """
    nodes = []
    for module in index.modules.values():
        for constant in module.constants.values():
            value = constant.node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstring = value.value
            elif isinstance(value, ast.Call):
                docstring = "; ".join(
                    f"{kw.arg}: {kw.value.value}"
                    for kw in value.keywords
                    if kw.arg
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                )
            else:
                docstring = ""
            nodes.append(
                ConstantNode(
                    constant.qn,
                    constant.name,
                    str(module.path),
                    constant.node.lineno,
                    constant.node.end_lineno,
                    f"{constant.name} = {ast.unparse(value)}"[:200],
                    docstring,
                )
            )
    return nodes


def class_docstrings(index: Index) -> dict[str, str]:
    """docstring узлов классов: собственный docstring класса и его лейблы, по строке на лейбл."""
    return {
        cls.qn: "\n".join(filter(None, [ast.get_docstring(cls.node) or "", *cls.labels]))
        for module in index.modules.values()
        for cls in module.classes.values()
        if cls.labels
    }
