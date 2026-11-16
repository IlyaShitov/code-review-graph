"""
Запись в graph.db code-review-graph: переписывание голых рёбер, вставка рёбер, узлов-констант и
лейблов классов.
"""

import json
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Final

from code_review_graph.constants import CLAIMED_ENDPOINT_KEY

from .resolve import INSERTED_RULES, RULE_PERMISSION, ConstantNode, Resolved

RULE_CONTAINS: Final[str] = "contains"
CLAIMED_ENDPOINT_PATH: Final[str] = f"$.{CLAIMED_ENDPOINT_KEY}"


def _insert_constants(con: sqlite3.Connection, constants: list[ConstantNode], now: float) -> None:
    """Узлы `Constant` и рёбра `CONTAINS` от узла файла, как у классов и функций."""
    extra = json.dumps({"resolver": "django"})
    file_hashes: dict[str, str] = {}
    for constant in constants:
        if constant.file not in file_hashes:
            row = con.execute(
                "select file_hash from nodes where kind = 'File' and qualified_name = ?",
                (constant.file,),
            ).fetchone()
            file_hashes[constant.file] = row[0] if row else ""
        con.execute(
            "insert into nodes(kind, name, qualified_name, file_path, line_start, line_end, "
            "language, parent_name, "
            "params, return_type, modifiers, is_test, file_hash, extra, updated_at, signature, "
            "symbol, docstring, "
            "name_tokens) values ('Constant', ?, ?, ?, ?, ?, 'python', '', '', '', '', 0, ?, "
            "?, ?, ?, ?, ?, ?) "
            "on conflict(qualified_name) do update set "
            "name=excluded.name, file_path=excluded.file_path, "
            "line_start=excluded.line_start, line_end=excluded.line_end, "
            "file_hash=excluded.file_hash, extra=excluded.extra, updated_at=excluded.updated_at, "
            "signature=excluded.signature, symbol=excluded.symbol, "
            "docstring=excluded.docstring, name_tokens=excluded.name_tokens",
            (
                constant.name,
                constant.qn,
                constant.file,
                constant.line_start,
                constant.line_end,
                file_hashes[constant.file],
                extra,
                now,
                constant.signature,
                constant.name,
                constant.docstring,
                " ".join(part.capitalize() for part in constant.name.split("_") if part),
            ),
        )
        con.execute(
            "insert into edges(kind, source_qualified, target_qualified, file_path, line, "
            "extra, confidence, "
            "confidence_tier, updated_at, target_resolution) values ('CONTAINS', ?, ?, ?, ?, "
            "?, 1.0, 'INFERRED', ?, 'direct')",
            (
                constant.file,
                constant.qn,
                constant.file,
                constant.line_start,
                json.dumps({"resolver": "django", "rule": RULE_CONTAINS}),
                now,
            ),
        )


def apply(
    con: sqlite3.Connection,
    resolved: list[Resolved],
    constants: list[ConstantNode],
    class_docstrings: dict[str, str],
) -> tuple[Counter, dict[str, list[Resolved]]]:
    stats: Counter = Counter()
    samples: dict[str, list[Resolved]] = defaultdict(list)
    now = time.time()
    # Рёбра пересоздаются; идентификаторы существующих констант сохраняются,
    # чтобы обновление без изменений не сбрасывало их сообщества.
    stats["removed_previous_edges"] = con.execute(
        f"delete from edges where json_extract(extra, '$.resolver') = 'django' "
        f"and json_extract(extra, '$.rule') in ({', '.join('?' * (len(INSERTED_RULES) + 1))})",
        (*INSERTED_RULES, RULE_CONTAINS),
    ).rowcount
    stats["removed_previous_constants"] = con.execute(
        "delete from nodes where kind = 'Constant' and json_extract(extra, '$.resolver') = "
        "'django' and qualified_name not in (select value from json_each(?))",
        (json.dumps([constant.qn for constant in constants]),),
    ).rowcount
    _insert_constants(con, constants, now)
    stats["constants"] = len(constants)
    for qn, docstring in class_docstrings.items():
        stats["class_docstrings"] += con.execute(
            "update nodes set docstring = ? where kind = 'Class' and qualified_name = ?",
            (docstring, qn),
        ).rowcount
    nodes = {row[0] for row in con.execute("select qualified_name from nodes")}
    # `+kind` и `+target_resolution` в запросах ниже отключают индексы по этим колонкам: иначе
    # SQLite
    # перебирает все рёбра этого вида или все неразрешённые вместо рёбер одного файла
    for item in resolved:
        if item.target not in nodes:
            stats["no_target_node"] += 1
            samples["no_target_node"].append(item)
            continue
        extra = {"resolver": "django", "rule": item.rule}
        if item.rule in INSERTED_RULES:
            # фильтр прав вызывает метод кверисета; константа и запись beat — ссылки
            kind = "CALLS" if item.rule == RULE_PERMISSION else "REFERENCES"
            # источник — самый узкий узел, в который попадает строка: метод, функция, класс
            # (тело класса) или константа (значение константы, рецепта)
            source = con.execute(
                "select qualified_name from nodes where file_path = ? and kind in ('Function', "
                "'Test', 'Class', 'Constant') "
                "and line_start <= ? and line_end >= ? order by line_end - line_start limit 1",
                (item.file, item.line, item.line),
            ).fetchone()
            # то же ребро от парсера: привязанное или ещё голое (`base`) — такое постобработка
            # привяжет к `claimed_endpoint` уже после обогащения
            existing = con.execute(
                "select id, source_qualified, target_qualified from edges where +kind = ? and "
                "file_path = ? and line = ? and (target_qualified = ? or "
                "(+target_resolution = 'unresolved' and json_extract(extra, ?) = ?))",
                (kind, item.file, item.line, item.target, CLAIMED_ENDPOINT_PATH, item.target),
            ).fetchone()
            if existing:
                edge_id, edge_source, edge_target = existing
                # на уровне модуля парсер ведёт ребро от файла: узла под значение константы у
                # него нет
                new_source = source[0] if source and edge_source == item.file else edge_source
                if (new_source, edge_target) == (edge_source, item.target):
                    stats["insert_skipped_exists"] += 1
                    continue
                con.execute(
                    "update edges set source_qualified = ?, target_qualified = ?, "
                    "target_resolution = 'direct', extra = json_remove(extra, ?) where id = ?",
                    (new_source, item.target, CLAIMED_ENDPOINT_PATH, edge_id),
                )
                stats["existing_rebound"] += 1
                continue
            if item.via:
                extra["via"] = item.via
            con.execute(
                "insert into edges(kind, source_qualified, target_qualified, file_path, line, "
                "extra, confidence, "
                "confidence_tier, updated_at, target_resolution) values (?, ?, ?, ?, ?, ?, "
                "1.0, 'INFERRED', ?, 'direct')",
                (
                    kind,
                    source[0] if source else item.file,
                    item.target,
                    item.file,
                    item.line,
                    json.dumps(extra),
                    now,
                ),
            )
            stats[f"inserted:{item.rule}"] += 1
            continue
        # голое имя (`m`) или вызов, который граф привязал к классу получателя (`<file>::Cls.m`),
        # хотя метод унаследован: `self.m()` и `x.m()` при аннотированном `x: Cls`
        candidates = con.execute(
            "select id, extra, target_qualified from edges where +kind = 'CALLS' and file_path "
            "= ? and line = ? "
            "and +target_resolution = 'unresolved'",
            (item.file, item.line),
        ).fetchall()
        name = item.name
        rows = []
        for edge_id, old_extra, target in candidates:
            old = json.loads(old_extra or "{}")
            bound_to_receiver = (
                old.get("receiver") == "self" or old.get("receiver_resolution") == "typed_receiver"
            )
            if target == name or (target.endswith(f".{name}") and bound_to_receiver):
                rows.append((edge_id, old))
        for edge_id, old in rows:
            con.execute(
                "update edges set target_qualified = ?, target_resolution = 'direct', extra = "
                "?, updated_at = ? "
                "where id = ?",
                (item.target, json.dumps(old | extra), now, edge_id),
            )
        if rows:
            stats["rewritten"] += len(rows)
            continue
        resolved_before = con.execute(
            "select 1 from edges where +kind = 'CALLS' and file_path = ? and line = ? and "
            "target_qualified = ?",
            (item.file, item.line, item.target),
        ).fetchone()
        if resolved_before:
            stats["already_resolved"] += 1
        else:
            stats["edge_not_found"] += 1
            samples["edge_not_found"].append(item)
    return stats, samples
