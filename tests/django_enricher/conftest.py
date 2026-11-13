# ruff: noqa: E501 -- embedded project source and SQLite schema fixtures
"""
Фикстуры: синтетический Django-проект с site-packages, его граф и мини-база графа для инжектора.
"""

import sqlite3
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from code_review_graph.django_enricher.enrich import find_source_root, project_files
from code_review_graph.django_enricher.index import Index
from code_review_graph.django_enricher.resolve import Resolved, resolve_project


@pytest.fixture(scope="session")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    Проект `proj/apps/shop` и site-packages `lib`: менеджеры, кверисеты, фильтр прав, вызовы.
    `lib/django` — пустышка: Django в индекс не берётся, иначе кверисеты перестанут
    распознаваться.
    """
    root = tmp_path_factory.mktemp("src").resolve()
    files = {
        "lib/django/__init__.py": "",
        "lib/libpkg/__init__.py": "",
        "lib/libpkg/mixins.py": """
            from django.db import models


            class LibQuerySetMixin(models.QuerySet):
                def lib_method(self):
                    return self
        """,
        "tools.py": "",
        "proj/manage.py": "",
        "proj/apps/__init__.py": "",
        "proj/apps/shop/migrations/0001_initial.py": """
            from apps.shop.models import Item


            def forwards():
                return Item.objects.active()
        """,
        "proj/apps/shop/__init__.py": "",
        "proj/apps/shop/managers.py": """
            from typing import Self

            from django.db import models
            from django.db.models import Manager, QuerySet
            from libpkg.mixins import LibQuerySetMixin


            class ItemQuerySet(LibQuerySetMixin, QuerySet):
                def filter_by_shop(self, shop_id) -> Self:
                    return self.filter(shop_id=shop_id)

                def active(self):
                    return self.filter(active=True).order_by('id')

                def ids(self) -> list[int]:
                    return list(self.values_list('id', flat=True))

                def to_orders(self) -> 'OrderQuerySet':
                    return OrderQuerySet(model=None)


            class SpecialItemQuerySet(ItemQuerySet):
                def special(self) -> Self:
                    return self


            class OrderQuerySet(models.QuerySet):
                def filter_by_user(self, user_id: int) -> Self:
                    return self.filter(user_id=user_id)

                def active(self) -> Self:
                    return self.filter(active=True)


            class OrderManager(Manager.from_queryset(OrderQuerySet)):
                pass


            class LegacyManager(Manager):
                def get_queryset(self):
                    return SpecialItemQuerySet(self.model, using=self._db)
        """,
        "proj/apps/shop/models.py": """
            from django.db import models

            from apps.shop.managers import ItemQuerySet, LegacyManager, OrderManager


            class ItemStatus(models.TextChoices):
                ACTIVE = 'ACTIVE', 'Активен'


            class Item(models.Model):
                objects = ItemQuerySet.as_manager()
                legacy = LegacyManager()
                title = models.CharField(verbose_name='Название')

                class Meta:
                    verbose_name = 'Товар'


            class Order(models.Model):
                objects = OrderManager()
        """,
        "proj/apps/shop/structs.py": """
            class NamedPermission:
                pass
        """,
        "proj/apps/shop/permissions.py": """
            from apps.shop.structs import NamedPermission

            SHOP_FULL_PERMISSION = NamedPermission(app_label='shop', verbose_name='Может смотреть все магазины')
            SHOP_TITLE = 'Магазины'
            ALL_PERMISSIONS = [SHOP_FULL_PERMISSION]
        """,
        "proj/apps/shop/report.py": '''
            from apps.shop import permissions
            from apps.shop.permissions import SHOP_FULL_PERMISSION


            class ShopReport:
                """Отчёт по магазинам."""

                name = 'Отчёт "Магазины"'
                report_permission = (SHOP_FULL_PERMISSION.full_permission,)

                def title(self) -> str:
                    return permissions.SHOP_TITLE


            def local_title() -> str:
                SHOP_TITLE = 'локальный'
                return SHOP_TITLE
        ''',
        "proj/apps/shop/filters.py": """
            from dataclasses import dataclass

            from django.db import models


            @dataclass
            class BaseFilter:
                queryset: models.QuerySet
                user: object

                def filter_by_user(self):
                    return self.queryset.filter_by_user(self.user.id)


            @dataclass
            class OrderFilter(BaseFilter):
                pass


            @dataclass
            class ActiveOrderFilter(BaseFilter):
                def filter_by_user(self):
                    return super().filter_by_user().active()
        """,
        "proj/apps/shop/usage.py": """
            from apps.shop.filters import ActiveOrderFilter, OrderFilter
            from apps.shop.managers import ItemQuerySet, OrderQuerySet
            from apps.shop.models import Item, Order


            def chain():
                return Item.objects.active().filter(x=1).filter_by_shop(1)


            def multiline():
                return (
                    Item.objects.active()
                    .filter_by_shop(1)
                )


            def variable(qs: ItemQuerySet):
                other = Item.objects.all()
                other = other.filter_by_shop(2)
                return qs.filter_by_shop(3), other.active()


            def annotated_var():
                queryset: OrderQuerySet = Order.objects.all()
                return queryset.filter_by_user(1)


            def custom_manager():
                return Order.objects.filter_by_user(5), Item.legacy.special(), Item.legacy.filter_by_shop(1)


            def inherited_lib():
                return Item.objects.lib_method().filter_by_shop(9)


            def not_a_queryset(x):
                a = Item.objects.ids().filter_by_shop(1)
                b = Item.objects.values('id').filter_by_shop(1)
                c = Item.objects.unknown().filter_by_shop(1)
                y = Item.objects.all() if x else Order.objects.all()
                d = y.filter_by_shop(1)
                z = Item.objects.all()
                z = Order.objects.all()
                e = z.filter_by_user(1)
                return a, b, c, d, e


            def to_other():
                return Item.objects.to_orders().filter_by_user(1)


            def permission():
                return OrderFilter(queryset=Order.objects.filter_by_user(1), user=None).execute()


            def permission_positional():
                return OrderFilter(Order.objects.all(), None)


            def permission_super():
                return ActiveOrderFilter(queryset=Order.objects.all(), user=None)


            class Service:
                def helper(self) -> int:
                    return 1


            class SubService(Service):
                def run(self):
                    return self.helper(), super().helper()
        """,
    }
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return root


@pytest.fixture(scope="session")
def project_graph(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[sqlite3.Connection]:
    """Граф проекта: узлы `File` всех файлов `proj`, включая миграцию, и `tools.py` вне корня импортов."""
    con = sqlite3.connect(tmp_path_factory.mktemp("graph") / "graph.db")
    con.execute("create table nodes (kind text, file_path text, language text)")
    con.executemany(
        "insert into nodes values ('File', ?, 'python')",
        [
            (str(path),)
            for path in [*sorted((workspace / "proj").rglob("*.py")), workspace / "tools.py"]
        ],
    )
    yield con
    con.close()


@pytest.fixture(scope="session")
def index(workspace: Path, project_graph: sqlite3.Connection) -> Index:
    """Индекс, собранный как в `enrich.main`."""
    root = find_source_root(workspace)
    return Index(root, project_files(project_graph, root), workspace / "lib")


@pytest.fixture(scope="session")
def resolved(index: Index) -> list[Resolved]:
    results, _ = resolve_project(index)
    return results


@pytest.fixture
def graph_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Класс `Q` с методами `m`/`n`, функция-источник `f`, файл `/p/c.py` под константу;
    голое ребро `m` (стр. 3) и `self.n()`, привязанный графом к `Sub` (стр. 9). Схема `nodes` — как у графа."""
    con = sqlite3.connect(tmp_path / "graph.db")
    con.executescript("""
        create table nodes (
            id integer primary key autoincrement, kind text not null, name text not null,
            qualified_name text not null unique, file_path text not null, line_start integer, line_end integer,
            language text, parent_name text, params text, return_type text, modifiers text,
            is_test integer default 0, file_hash text, extra text default '{}', updated_at real not null,
            signature text, community_id integer, symbol text, docstring text, name_tokens text
        );
        create table edges (
            id integer primary key autoincrement, kind text not null, source_qualified text not null,
            target_qualified text not null, file_path text not null, line integer default 0,
            extra text default '{}', confidence real default 1.0, confidence_tier text default 'EXTRACTED',
            updated_at real not null, target_resolution text
        );
        insert into nodes(kind, name, qualified_name, file_path, line_start, line_end, file_hash, updated_at, docstring) values
            ('Class', 'Q', '/p/a.py::Q', '/p/a.py', 1, 4, 'ha', 0, ''),
            ('Function', 'm', '/p/a.py::Q.m', '/p/a.py', 1, 2, 'ha', 0, ''),
            ('Function', 'n', '/p/a.py::Q.n', '/p/a.py', 3, 4, 'ha', 0, ''),
            ('Function', 'f', '/p/b.py::f', '/p/b.py', 1, 20, 'hb', 0, ''),
            ('File', 'c.py', '/p/c.py', '/p/c.py', 1, 1, 'hc', 0, '');
        insert into edges(kind, source_qualified, target_qualified, file_path, line, extra, updated_at, target_resolution) values
            ('CALLS', '/p/b.py::f', 'm', '/p/b.py', 3, '{"receiver_form": "expression"}', 0, 'unresolved'),
            ('CALLS', '/p/b.py::f', '/p/b.py::Sub.n', '/p/b.py', 9, '{"receiver": "self"}', 0, 'unresolved');
    """)
    yield con
    con.close()
