from pathlib import Path

from code_review_graph.django_enricher.index import Index
from code_review_graph.django_enricher.resolve import (
    RULE_CHAIN,
    RULE_CONSTANT,
    RULE_MRO,
    RULE_PERMISSION,
    Resolved,
    class_docstrings,
    constant_nodes,
)


def test_resolved_edges(resolved: list[Resolved]) -> None:
    """
    Точный набор: всё, что выводится однозначно, и ничего из `not_a_queryset` после разрыва
    цепочки.
    """
    got = {
        (r.scope, r.name, r.target.split("::")[1], r.rule, r.via)
        for r in resolved
        if r.rule != RULE_CONSTANT
    }

    assert got == {
        ("chain", "active", "ItemQuerySet.active", RULE_CHAIN, ""),
        ("chain", "filter_by_shop", "ItemQuerySet.filter_by_shop", RULE_CHAIN, ""),
        ("multiline", "active", "ItemQuerySet.active", RULE_CHAIN, ""),
        ("multiline", "filter_by_shop", "ItemQuerySet.filter_by_shop", RULE_CHAIN, ""),
        ("variable", "filter_by_shop", "ItemQuerySet.filter_by_shop", RULE_CHAIN, ""),
        ("variable", "active", "ItemQuerySet.active", RULE_CHAIN, ""),
        ("annotated_var", "filter_by_user", "OrderQuerySet.filter_by_user", RULE_CHAIN, ""),
        ("custom_manager", "filter_by_user", "OrderQuerySet.filter_by_user", RULE_CHAIN, ""),
        ("custom_manager", "special", "SpecialItemQuerySet.special", RULE_CHAIN, ""),
        ("custom_manager", "filter_by_shop", "ItemQuerySet.filter_by_shop", RULE_CHAIN, ""),
        ("inherited_lib", "filter_by_shop", "ItemQuerySet.filter_by_shop", RULE_CHAIN, ""),
        ("not_a_queryset", "ids", "ItemQuerySet.ids", RULE_CHAIN, ""),
        ("to_other", "to_orders", "ItemQuerySet.to_orders", RULE_CHAIN, ""),
        ("to_other", "filter_by_user", "OrderQuerySet.filter_by_user", RULE_CHAIN, ""),
        ("permission", "filter_by_user", "OrderQuerySet.filter_by_user", RULE_CHAIN, ""),
        (
            "permission",
            "filter_by_user",
            "OrderQuerySet.filter_by_user",
            RULE_PERMISSION,
            "OrderFilter",
        ),
        (
            "permission_positional",
            "filter_by_user",
            "OrderQuerySet.filter_by_user",
            RULE_PERMISSION,
            "OrderFilter",
        ),
        (
            "permission_super",
            "filter_by_user",
            "OrderQuerySet.filter_by_user",
            RULE_PERMISSION,
            "ActiveOrderFilter",
        ),
        (
            "permission_super",
            "active",
            "OrderQuerySet.active",
            RULE_PERMISSION,
            "ActiveOrderFilter",
        ),
        ("SubService.run", "helper", "Service.helper", RULE_MRO, ""),
        (
            "ActiveOrderFilter.filter_by_user",
            "filter_by_user",
            "BaseFilter.filter_by_user",
            RULE_MRO,
            "",
        ),
    }


def test_chain_edges_use_chain_start_line(resolved: list[Resolved], workspace: Path) -> None:
    """Граф пишет все вызовы цепочки на строку её начала — по ней инжектор ищет ребро."""
    lines = (workspace / "proj/apps/shop/usage.py").read_text().splitlines()
    chain_start = next(
        number for number, text in enumerate(lines, 1) if text.strip() == "Item.objects.active()"
    )

    assert {r.line for r in resolved if r.scope == "multiline"} == {chain_start}


def test_constant_refs(resolved: list[Resolved]) -> None:
    """
    Импорт, `module.CONST`, свой модуль и тело класса — да; локальная переменная с тем же именем
    — нет. Рецепты baker — так же: `foreign_key(x)`, `x.extend()`, `recipes.x.make()`.
    """
    got = {
        (Path(r.file).name, r.line, r.scope, r.target.split("/")[-1])
        for r in resolved
        if r.rule == RULE_CONSTANT
    }

    assert got == {
        ("permissions.py", 5, "", "permissions.py::SHOP_FULL_PERMISSION"),
        ("report.py", 9, "ShopReport", "permissions.py::SHOP_FULL_PERMISSION"),
        ("report.py", 12, "ShopReport.title", "permissions.py::SHOP_TITLE"),
        ("baker_recipes.py", 8, "", "baker_recipes.py::order"),
        ("baker_recipes.py", 10, "", "baker_recipes.py::item"),
        ("recipes_usage.py", 4, "", "baker_recipes.py::item"),
        ("recipes_usage.py", 8, "make_items", "baker_recipes.py::cheap_item"),
        ("recipes_usage.py", 9, "make_items", "baker_recipes.py::item"),
    }


def test_labels(index: Index) -> None:
    """
    Лейблы уходят в docstring: у константы — строковые аргументы значения, у класса — по строке
    на лейбл.
    """
    constants = {c.name: (c.signature, c.docstring) for c in constant_nodes(index)}
    docstrings = {qn.split("::")[1]: docstring for qn, docstring in class_docstrings(index).items()}

    assert constants == {
        "SHOP_FULL_PERMISSION": (
            "SHOP_FULL_PERMISSION = NamedPermission(app_label='shop', verbose_name='Может "
            "смотреть все магазины')",
            "app_label: shop; verbose_name: Может смотреть все магазины",
        ),
        "SHOP_TITLE": ("SHOP_TITLE = 'Магазины'", "Магазины"),
        "ALL_PERMISSIONS": ("ALL_PERMISSIONS = [SHOP_FULL_PERMISSION]", ""),
        "order": ("order = Recipe(Order)", ""),
        "item": ("item = Recipe(Item, order=foreign_key(order))", ""),
        "cheap_item": ("cheap_item = item.extend(title='Дешёвый')", "title: Дешёвый"),
    }
    assert docstrings == {
        "ShopReport": 'Отчёт по магазинам.\nname: Отчёт "Магазины"',
        "ItemStatus": "ACTIVE: ACTIVE, Активен",
        "Item": "title: Название\nMeta.verbose_name: Товар",
    }
