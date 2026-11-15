"""Индекс классов проекта и сторонних библиотек: базы, C3-MRO, методы, менеджеры моделей."""

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# методы Django QuerySet, которые возвращают кверисет того же типа
QS_PRESERVING: Final[frozenset[str]] = frozenset(
    {
        "filter",
        "exclude",
        "annotate",
        "alias",
        "all",
        "order_by",
        "select_related",
        "prefetch_related",
        "distinct",
        "only",
        "defer",
        "using",
        "union",
        "none",
        "select_for_update",
        "extra",
        "reverse",
    }
)
SELF_ANNOTATIONS: Final[frozenset[str]] = frozenset(
    {"Self", "typing.Self", "typing_extensions.Self"}
)
# Django остаётся внешним именем: распознавание кверисетов держится на его базовом `QuerySet`
OPAQUE_PACKAGES: Final[frozenset[str]] = frozenset({"django"})
BAKER_RECIPE: Final[str] = "model_bakery.recipe.Recipe"


def walk_local(node: ast.AST) -> Iterator[ast.AST]:
    """Обходит потомков узла, не заходя внутрь вложенных функций, классов и лямбд."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(child))


def call_chain(call: ast.Call) -> tuple[ast.expr, list[str]]:
    """`a.b(...).c(...)` → (a, ['b', 'c']): цепочка вызовов методов от корня."""
    names = []
    expr: ast.expr = call
    while isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        names.append(expr.func.attr)
        expr = expr.func.value
    return expr, names[::-1]


def _is_self(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Name) and expr.id == "self"


def _dotted_name(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        head = _dotted_name(expr.value)
        return f"{head}.{expr.attr}" if head else None
    return None


def _is_recipe(value: ast.expr, imports: dict[str, str]) -> bool:
    """
    Рецепт model_bakery: `Recipe(...)` или `<рецепт>.extend(...)` в модуле, импортирующем
    `Recipe`. База `extend` не проверяется: присваивать результат `list.extend` бессмысленно.
    Ссылки на рецепт (`recipes.x.make()`, `foreign_key(x)`) разрешаются как на константу.
    """
    if not isinstance(value, ast.Call) or BAKER_RECIPE not in imports.values():
        return False
    func = value.func
    if isinstance(func, ast.Name):
        return imports.get(func.id) == BAKER_RECIPE
    return isinstance(func, ast.Attribute) and func.attr == "extend"


def _self_return_chains(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[list[str]] | None:
    """Имена методов в цепочках `return self.….…()`; None — если какой-то return не от self."""
    returns = [node for node in walk_local(fn) if isinstance(node, ast.Return)]
    if not returns:
        return None
    chains = []
    for ret in returns:
        if ret.value is None:
            return None
        if _is_self(ret.value):
            chains.append([])
            continue
        if not isinstance(ret.value, ast.Call):
            return None
        root, names = call_chain(ret.value)
        if not _is_self(root):
            return None
        chains.append(names)
    return chains


def _key(item: "ClassInfo | str") -> str:
    return item if isinstance(item, str) else item.qn


def _c3_merge(seqs: list[list["ClassInfo | str"]]) -> list["ClassInfo | str"] | None:
    seqs = [list(seq) for seq in seqs if seq]
    result = []
    while seqs:
        for seq in seqs:
            head = seq[0]
            if not any(_key(head) in {_key(x) for x in other[1:]} for other in seqs):
                break
        else:
            return None
        result.append(head)
        seqs = [[x for x in seq if _key(x) != _key(head)] for seq in seqs]
        seqs = [seq for seq in seqs if seq]
    return result


@dataclass
class MethodInfo:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    returns_self: bool = False


@dataclass(eq=False)
class ClassInfo:
    qn: str
    name: str
    module: str
    is_project: bool
    node: ast.ClassDef
    methods: dict[str, MethodInfo] = field(default_factory=dict)
    field_anns: dict[str, ast.expr] = field(default_factory=dict)
    # атрибуты класса, которым присвоен вызов: среди них менеджеры моделей (`objects =
    # X.as_manager()`)
    call_attrs: dict[str, ast.Call] = field(default_factory=dict)
    # метод → цепочки вызовов от `self.queryset` в его теле
    queryset_chains: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    # метод → цепочки от `super()`: первым идёт вызываемый родительский метод (`super().m().x()` →
    # ('m', 'x'))
    super_chains: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    # лейблы класса: `name`/`title`, `verbose_name` полей, `Meta.verbose_name`, подписи choices
    labels: list[str] = field(default_factory=list)


@dataclass
class ConstantInfo:
    qn: str
    name: str
    module: str
    node: ast.Assign | ast.AnnAssign


@dataclass
class ModuleInfo:
    name: str
    path: Path
    is_project: bool
    tree: ast.Module
    imports: dict[str, str] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    constants: dict[str, ConstantInfo] = field(default_factory=dict)


class Index:
    def __init__(
        self, source_root: Path, project_files: list[Path], site_packages: Path | None = None
    ):
        self.modules: dict[str, ModuleInfo] = {}
        self.by_dotted: dict[str, ClassInfo] = {}
        self.errors: list[str] = []
        self._mro_cache: dict[str, list[ClassInfo | str] | None] = {}
        self._managers_cache: dict[str, dict[str, ClassInfo]] = {}
        self._site_packages = site_packages
        self._lib_tried: set[str] = set()
        self._lib_heads: dict[str, bool] = {}
        for path in sorted(project_files):
            self._load(path, source_root, is_project=True)
        self.roots = {name.split(".")[0] for name in self.modules}

    def _is_lib(self, head: str) -> bool:
        """Пакет верхнего уровня из site-packages, который разбирается (Django — нет)."""
        if head not in self._lib_heads:
            self._lib_heads[head] = (
                self._site_packages is not None
                and head not in OPAQUE_PACKAGES
                and head not in self.roots
                and (
                    (self._site_packages / head).is_dir()
                    or (self._site_packages / f"{head}.py").is_file()
                )
            )
        return self._lib_heads[head]

    def _lib_module(self, name: str) -> ModuleInfo | None:
        """
        Модуль библиотеки разбирается при первом обращении: библиотеки целиком разбирать долго.
        """
        site_packages = self._site_packages
        if (
            site_packages is not None
            and name not in self._lib_tried
            and self._is_lib(name.split(".")[0])
        ):
            self._lib_tried.add(name)
            base = site_packages.joinpath(*name.split("."))
            path = next(
                (p for p in (base / "__init__.py", base.with_suffix(".py")) if p.is_file()), None
            )
            if path is not None:
                self._load(path, site_packages, is_project=False)
        return self.modules.get(name)

    # --- загрузка ---------------------------------------------------------------------------------

    def _load(self, path: Path, root: Path, is_project: bool) -> None:
        if (path.with_suffix("") / "__init__.py").exists():
            # `x.py` рядом с пакетом `x/`: Python импортирует пакет, модуль мёртвый
            return
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
            self.errors.append(f"{path}: {exc}")
            return
        parts = list(path.relative_to(root).with_suffix("").parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        module = ModuleInfo(".".join(parts), path, is_project, tree)
        self._collect_imports(module, is_pkg)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                qn = f"{path}::{node.name}" if is_project else f"lib:{module.name}.{node.name}"
                cls = ClassInfo(qn, node.name, module.name, is_project, node)
                self._collect_class(cls)
                module.classes[node.name] = cls
                self.by_dotted[f"{module.name}.{node.name}"] = cls
            elif is_project and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (
                    len(targets) == 1
                    and isinstance(targets[0], ast.Name)
                    and node.value is not None
                    and (
                        re.fullmatch(r"_?[A-Z][A-Z0-9_]+", targets[0].id)
                        or _is_recipe(node.value, module.imports)
                    )
                ):
                    name = targets[0].id
                    module.constants[name] = ConstantInfo(
                        f"{path}::{name}", name, module.name, node
                    )
        self.modules[module.name] = module

    def _collect_imports(self, module: ModuleInfo, is_pkg: bool) -> None:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        module.imports[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".")[0]
                        module.imports.setdefault(head, head)
            elif isinstance(node, ast.ImportFrom):
                base = self._import_base(module.name, is_pkg, node)
                for alias in node.names:
                    if alias.name == "*":
                        module.stars.append(base)
                    else:
                        module.imports[alias.asname or alias.name] = (
                            f"{base}.{alias.name}" if base else alias.name
                        )

    @staticmethod
    def _import_base(module_name: str, is_pkg: bool, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        parts = module_name.split(".") if module_name else []
        if not is_pkg:
            parts = parts[:-1]
        if node.level > 1:
            parts = parts[: len(parts) - (node.level - 1)]
        if node.module:
            parts.append(node.module)
        return ".".join(parts)

    def _collect_class(self, cls: ClassInfo) -> None:
        for node in cls.node.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls.methods[node.name] = MethodInfo(node)
                chains: set[tuple[str, ...]] = set()
                super_chains: set[tuple[str, ...]] = set()
                for call in walk_local(node):
                    if not isinstance(call, ast.Call):
                        continue
                    root, names = call_chain(call)
                    if not names:
                        continue
                    if (
                        isinstance(root, ast.Attribute)
                        and root.attr == "queryset"
                        and _is_self(root.value)
                    ):
                        chains.add(tuple(names))
                    elif (
                        isinstance(root, ast.Call)
                        and _dotted_name(root.func) == "super"
                        and not root.args
                    ):
                        super_chains.add(tuple(names))
                if chains:
                    cls.queryset_chains[node.name] = chains
                if super_chains:
                    cls.super_chains[node.name] = super_chains
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                cls.field_anns[node.target.id] = node.annotation
                if isinstance(node.value, ast.Call):
                    cls.call_attrs[node.target.id] = node.value
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
            ):
                cls.call_attrs[node.targets[0].id] = node.value
        self._mark_returns_self(cls)
        if cls.is_project:
            self._collect_labels(cls)

    @staticmethod
    def _collect_labels(cls: ClassInfo) -> None:
        # keyword-аргументы с человекочитаемым текстом: поля моделей, сериализаторов, фильтров
        label_kwargs = {"verbose_name", "verbose_name_plural", "label", "help_text", "title"}
        is_choices = any(ast.unparse(base).endswith("Choices") for base in cls.node.bases)
        meta = next(
            (n for n in cls.node.body if isinstance(n, ast.ClassDef) and n.name == "Meta"), None
        )
        assignments = [(n, "") for n in cls.node.body] + [
            (n, "Meta.") for n in (meta.body if meta else [])
        ]
        for node, prefix in assignments:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                attr, value = node.targets[0].id, node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                attr, value = node.target.id, node.value
            else:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if attr in {"name", "title", "verbose_name", "verbose_name_plural", "layer_name"}:
                    cls.labels.append(f"{prefix}{attr}: {value.value}")
            elif isinstance(value, ast.Tuple) and is_choices:
                texts = [
                    e.value
                    for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if texts:
                    cls.labels.append(f"{attr}: {', '.join(texts)}")
            elif isinstance(value, ast.Call):
                texts = [
                    kw.value.value
                    for kw in value.keywords
                    if kw.arg in label_kwargs
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ]
                # verbose_name поля модели первым позиционным аргументом:
                # `models.CharField('Название')`
                if (
                    ast.unparse(value.func).endswith("Field")
                    and value.args
                    and isinstance(value.args[0], ast.Constant)
                    and isinstance(value.args[0].value, str)
                ):
                    texts.insert(0, value.args[0].value)
                if texts:
                    cls.labels.append(f"{attr}: {'; '.join(texts)}")

    @staticmethod
    def _mark_returns_self(cls: ClassInfo) -> None:
        pending: dict[str, list[list[str]]] = {}
        for name, method in cls.methods.items():
            returns = method.node.returns
            if returns is not None:
                method.returns_self = ast.unparse(returns).strip("'\"") in SELF_ANNOTATIONS | {
                    cls.name
                }
            elif (chains := _self_return_chains(method.node)) is not None:
                pending[name] = chains
        marked = set(pending)
        changed = True
        while changed:
            changed = False
            allowed = QS_PRESERVING | marked | {n for n, m in cls.methods.items() if m.returns_self}
            for name in list(marked):
                if any(step not in allowed for chain in pending[name] for step in chain):
                    marked.discard(name)
                    changed = True
        for name in marked:
            cls.methods[name].returns_self = True

    # --- разрешение имён ------------------------------------------------------------------------

    def resolve_dotted(self, dotted: str, depth: int = 0) -> ClassInfo | ConstantInfo | str | None:
        """
        Класс или константа проекта либо библиотеки; строка — внешнее имя (Django, stdlib); None
        — не разрешилось.
        """
        if dotted in self.by_dotted:
            return self.by_dotted[dotted]
        if depth > 8:
            return None
        module_name, _, name = dotted.rpartition(".")
        module = self.modules.get(module_name) or self._lib_module(module_name)
        if module is None:
            # неразобранное внутри проекта или библиотеки — пропуск, остальное — внешнее имя
            # (Django, stdlib)
            head = dotted.split(".")[0]
            return None if head in self.roots or self._is_lib(head) else dotted
        if name in module.constants:
            return module.constants[name]
        if name in module.imports:
            return self.resolve_dotted(module.imports[name], depth + 1)
        for star in module.stars:
            found = self.resolve_dotted(f"{star}.{name}", depth + 1)
            if isinstance(found, (ClassInfo, ConstantInfo)):
                return found
        return None

    def resolve_expr(
        self, module: ModuleInfo, expr: ast.expr | None
    ) -> ClassInfo | ConstantInfo | str | None:
        if expr is None:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                expr = ast.parse(expr.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(expr, ast.Subscript):
            if _dotted_name(expr.value) in ("Optional", "typing.Optional"):
                return self.resolve_expr(module, expr.slice)
            return self.resolve_expr(module, expr.value)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            sides = [
                s
                for s in (expr.left, expr.right)
                if not (isinstance(s, ast.Constant) and s.value is None)
            ]
            return self.resolve_expr(module, sides[0]) if len(sides) == 1 else None
        dotted = _dotted_name(expr)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        if head in module.classes or head in module.constants:
            target = f"{module.name}.{head}"
        elif head in module.imports:
            target = module.imports[head]
        elif head == "object":
            target = "builtins.object"
        else:
            return None
        return self.resolve_dotted(f"{target}.{rest}" if rest else target)

    # --- иерархия --------------------------------------------------------------------------------

    def _bases(self, cls: ClassInfo) -> list[ClassInfo | str]:
        module = self.modules[cls.module]
        bases: list[ClassInfo | str] = []
        for position, base in enumerate(cls.node.bases):
            resolved = None if isinstance(base, ast.Call) else self.resolve_expr(module, base)
            bases.append(
                resolved if isinstance(resolved, (ClassInfo, str)) else f"?{cls.qn}:{position}"
            )
        return bases

    def mro(
        self, cls: ClassInfo, _seen: frozenset[str] = frozenset()
    ) -> list[ClassInfo | str] | None:
        if cls.qn in self._mro_cache:
            return self._mro_cache[cls.qn]
        if cls.qn in _seen:
            return None
        bases = self._bases(cls)
        seqs: list[list[ClassInfo | str]] = []
        for base in bases:
            if isinstance(base, ClassInfo):
                base_mro = self.mro(base, _seen | {cls.qn})
                if base_mro is None:
                    self._mro_cache[cls.qn] = None
                    return None
                seqs.append(list(base_mro))
            else:
                seqs.append([base])
        seqs.append(list(bases))
        merged = _c3_merge(seqs)
        result = None if merged is None else [cls, *merged]
        self._mro_cache[cls.qn] = result
        return result

    def find_method(
        self, cls: ClassInfo, name: str, after: str = ""
    ) -> tuple[ClassInfo, MethodInfo] | None:
        """Метод по MRO; `after` — qn класса, после которого искать (для `super()`)."""
        order = self.mro(cls)
        if order is None:
            return None
        if after:
            keys = [_key(item) for item in order]
            if after not in keys:
                return None
            order = order[keys.index(after) + 1 :]
        # внешние базы, за которыми точно нет методов кверисета или проекта
        transparent = {"typing.Generic", "abc.ABC", "builtins.object", "typing.Protocol"}
        for item in order:
            if isinstance(item, str):
                if item in transparent:
                    continue
                return None
            if name in item.methods:
                return item, item.methods[name]
        return None

    def is_queryset(self, cls: ClassInfo) -> bool:
        order = self.mro(cls) or []
        return any(
            isinstance(item, str) and item.rsplit(".", 1)[-1] == "QuerySet" for item in order
        )

    def _classes_in_mro(self, cls: ClassInfo) -> list[ClassInfo]:
        return [item for item in (self.mro(cls) or [cls]) if isinstance(item, ClassInfo)]

    def field_ann(self, cls: ClassInfo, attr: str) -> tuple[ClassInfo, ast.expr] | None:
        for item in self._classes_in_mro(cls):
            if attr in item.field_anns:
                return item, item.field_anns[attr]
        return None

    def field_order(self, cls: ClassInfo) -> list[str]:
        names: list[str] = []
        for item in reversed(self._classes_in_mro(cls)):
            names.extend(name for name in item.field_anns if name not in names)
        return names

    def queryset_chains(self, cls: ClassInfo) -> set[tuple[str, ...]]:
        """Цепочки от `self.queryset` в методах, которые реально действуют по MRO (без перекрытых),
        с разворачиванием `super()`."""
        order = self._classes_in_mro(cls)
        seen: set[str] = set()
        result: set[tuple[str, ...]] = set()
        for position, item in enumerate(order):
            for method_name in item.methods:
                if method_name not in seen:
                    seen.add(method_name)
                    result |= self._method_chains(order, position, method_name)
        return result

    def _method_chains(self, order: list[ClassInfo], start: int, name: str) -> set[tuple[str, ...]]:
        """
        Цепочки первой по MRO с `order[start]` реализации метода; `super().m().x()` в ней
        разворачивается в цепочки родительского `m`, продолженные `x`.
        """
        for position in range(start, len(order)):
            owner = order[position]
            if name in owner.methods:
                chains = set(owner.queryset_chains.get(name, set()))
                for parent_name, *rest in owner.super_chains.get(name, set()):
                    chains |= {
                        (*chain, *rest)
                        for chain in self._method_chains(order, position + 1, parent_name)
                    }
                return chains
        return set()

    # --- менеджеры -------------------------------------------------------------------------------

    def managers(self, cls: ClassInfo) -> dict[str, ClassInfo]:
        if cls.qn in self._managers_cache:
            return self._managers_cache[cls.qn]
        result: dict[str, ClassInfo] = {}
        for item in reversed(self._classes_in_mro(cls)):
            module = self.modules[item.module]
            for attr, call in item.call_attrs.items():
                queryset = self._manager_queryset(module, call)
                if queryset is not None:
                    result[attr] = queryset
                else:
                    result.pop(attr, None)
        self._managers_cache[cls.qn] = result
        return result

    def _manager_queryset(self, module: ModuleInfo, call: ast.Call) -> ClassInfo | None:
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "as_manager":
            queryset = self.resolve_expr(module, func.value)
            return queryset if isinstance(queryset, ClassInfo) else None
        if isinstance(func, ast.Call):
            return self._from_queryset_arg(module, func)
        manager = self.resolve_expr(module, func)
        return self._manager_class_queryset(manager) if isinstance(manager, ClassInfo) else None

    def _from_queryset_arg(self, module: ModuleInfo, call: ast.Call) -> ClassInfo | None:
        if isinstance(call.func, ast.Attribute) and call.func.attr == "from_queryset" and call.args:
            queryset = self.resolve_expr(module, call.args[0])
            return queryset if isinstance(queryset, ClassInfo) else None
        return None

    def _manager_class_queryset(self, manager: ClassInfo, depth: int = 0) -> ClassInfo | None:
        if depth > 5:
            return None
        module = self.modules[manager.module]
        for base in manager.node.bases:
            if isinstance(base, ast.Call) and (queryset := self._from_queryset_arg(module, base)):
                return queryset
        get_queryset = manager.methods.get("get_queryset")
        if get_queryset is not None:
            for node in walk_local(get_queryset.node):
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
                    queryset = self.resolve_expr(module, node.value.func)
                    if isinstance(queryset, ClassInfo) and self.is_queryset(queryset):
                        return queryset
        for base in manager.node.bases:
            if not isinstance(base, ast.Call):
                parent = self.resolve_expr(module, base)
                if isinstance(parent, ClassInfo) and (
                    queryset := self._manager_class_queryset(parent, depth + 1)
                ):
                    return queryset
        return None
