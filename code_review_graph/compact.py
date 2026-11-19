"""Human-readable MCP query responses; the underlying query contracts stay JSON."""

from __future__ import annotations

import json
from typing import Any

_NODE_FIELDS = {
    "id", "name", "parent_name", "qualified_name", "kind", "file_path",
    "line_start", "line_end", "language", "params", "return_type", "score",
    "is_test", "signature", "docstring", "call_site", "source", "callers", "callees",
    "constants", "import_target", "importer", "file",
}
_EDGE_FIELDS = {
    "id", "source", "target", "kind", "file_path", "line", "confidence_tier", "confidence",
    "rule",
}


def _value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _edge_details(edge: dict[str, Any]) -> list[str]:
    details = []
    if edge.get("rule"):
        # The rule says how a resolver inferred the edge; its tier and
        # confidence are the same for every edge of that rule.
        details.append(str(edge["rule"]))
    else:
        tier = edge.get("confidence_tier")
        if tier and tier != "EXTRACTED":
            details.append(str(tier))
        if edge.get("confidence", 1) < 1:
            details.append(f"confidence: {edge['confidence']}")
    details.extend(f"{key}: {_value(value)}" for key, value in edge.items()
                   if key not in _EDGE_FIELDS)
    return details


def _node_lines(
    node: dict[str, Any], prefix: str, edge: dict[str, Any] | None = None,
    *, full_name: bool = False,
) -> list[str]:
    qualified = (
        node.get("qualified_name") or node.get("import_target") or node.get("importer", "")
    )
    # A file's name is its path, which the label already shows.
    symbol = "" if node.get("kind") == "File" else (qualified.partition("::")[2] or ".".join(
        str(node[key]) for key in ("parent_name", "name") if node.get(key)
    ))
    path = node.get("file_path") or node.get("file") or qualified.partition("::")[0]
    identity = qualified or f"{path}  {symbol}".strip()
    location = str(node.get("kind", ""))
    if node.get("line_start") is not None:
        location += f" {node['line_start']}-{node.get('line_end', node['line_start'])}"
    if node.get("is_test"):
        location += ", test"

    site = node.get("call_site")
    if site and not full_name:
        site_path = site.get("file", path)
        label = f"{site_path.removeprefix(prefix)}:{site['line']}  {symbol}".rstrip()
        if site_path != path and node.get("file_path"):
            label += f" [definition: {identity.removeprefix(prefix)}]"
    else:
        label = identity if full_name else identity.removeprefix(prefix)
    details = [location.strip(" ,")] if location else []
    details.extend(f"{key}: {_value(value)}" for key, value in node.items()
                   if key not in _NODE_FIELDS)
    if edge:
        details.append(edge["kind"])
        if not site:
            details.append(f"{edge['file_path'].removeprefix(prefix)}:{edge['line']}")
        details.extend(detail for detail in _edge_details(edge) if detail not in details)
    if details:
        label += f"  ({', '.join(details)})"
    if node.get("signature"):
        label += f"  {_value(node['signature'])}"
    if node.get("docstring"):
        label += f" — {_value(node['docstring'])[:120]}"
    lines = [label]
    if node.get("source") is not None:
        lines.append(node["source"])
    if node.get("constants"):
        lines.append("constants:")
        lines.extend(
            f"{constant['file_path'].removeprefix(prefix)}:{constant['line']}  "
            f"{_value(constant['value'])}"
            for constant in node["constants"]
        )
    for relation in ("callers", "callees"):
        if node.get(relation):
            lines.append(f"{relation}:")
            for neighbor in node[relation]:
                lines.extend(_node_lines(neighbor, prefix))
    return lines


def _hop_line(hop: dict[str, Any], prefix: str) -> str:
    source = hop["from"].removeprefix(prefix)
    target = hop["to"].removeprefix(prefix)
    path = hop.get("file_path", hop.get("file", hop["from"].partition("::")[0]))
    details = ", ".join(f"{key}: {_value(value)}" for key, value in hop.items()
                        if key not in {"from", "to", "file_path", "file", "line"})
    return f"{source} -> {target}  {path.removeprefix(prefix)}:{hop.get('line', '?')} ({details})"


def format_compact(response: dict[str, Any], repo_root: str | None = None) -> str | dict:
    """Compact successful/ambiguous query output without dropping evidence.

    Only the MCP wrappers call this. Errors and unsupported shapes retain their
    original dictionary. Without an explicit root paths remain absolute, rather
    than guessing the root from the first result's directory names.
    """
    status = response.get("status")
    if status not in {"ok", "ambiguous"}:
        return response
    rows = response.get("candidates" if status == "ambiguous" else "results")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return response
    if any(not (
        row.get("qualified_name") or row.get("name") or row.get("import_target")
        or row.get("importer") or ("from" in row and "to" in row)
    ) for row in rows):
        return response
    prefix = repo_root.rstrip("/\\") + "/" if repo_root else ""
    summary = response.get("summary", status)
    lines = [summary.replace(prefix, "") if prefix else summary]
    if status == "ambiguous":
        lines[0] = f"ambiguous: {lines[0]}"
    if repo_root:
        lines.append(f"repo_root: {repo_root} (paths relative to this root)")
    if response.get("search_mode"):
        lines[0] += f" [search_mode: {response['search_mode']}]"

    edges = response.get("edges", [])
    used_edges: set[int] = set()
    for index, row in enumerate(rows):
        if "from" in row and "to" in row:
            lines.append(_hop_line(row, prefix))
            continue
        edge = None
        # Query edges are aligned with results when every row has an edge. Match
        # either end: a callee is the target, whereas a caller is the source.
        if len(edges) == len(rows):
            candidate = edges[index]
            identity = row.get("qualified_name") or row.get("import_target") or row.get("importer")
            if identity and identity in (candidate.get("source"), candidate.get("target")):
                edge = candidate
                used_edges.add(index)
        lines.extend(_node_lines(row, prefix, edge, full_name=status == "ambiguous"))
    for index, edge in enumerate(edges):
        if index not in used_edges:
            lines.append(
                f"edge: {edge.get('source', '').removeprefix(prefix)} -> "
                f"{edge.get('target', '').removeprefix(prefix)}  "
                f"{edge.get('file_path', '').removeprefix(prefix)}:{edge.get('line', '?')} "
                f"({edge.get('kind', '')}, {', '.join(_edge_details(edge))})"
            )

    # Keep bounds, inference notes and new query-specific metadata automatically.
    # These fields carry meaning; routine storage IDs, hints and fresh provenance
    # do not need to repeat the same JSON around each result.
    for key, value in response.items():
        if key in {"status", "summary", "description", "results", "candidates",
                   "edges", "search_mode", "_graph", "_hints"}:
            continue
        if key == "disambiguation" and value == response.get("candidates"):
            lines.append("disambiguation: candidates listed above")
            continue
        if key == "weakest_hop" and isinstance(value, dict):
            lines.append(f"weakest_hop: {_hop_line(value, prefix)}")
        elif value not in (None, False, 0, "") or key in {"result_count", "total"}:
            rendered = _value(value)
            if key in {"target", "to"}:
                rendered = rendered.removeprefix(prefix)
            lines.append(f"{key}: {rendered}")
    graph = response.get("_graph", {})
    if graph.get("head_matches_build") is False:
        lines.append(
            f"WARNING: graph built at {graph.get('built_at_sha')}, HEAD {graph.get('head_sha')}"
        )
    for key, value in graph.items():
        if key not in {"updated_at", "age_seconds", "built_at_sha", "built_on_branch",
                       "head_sha", "head_matches_build"}:
            lines.append(f"_graph.{key}: {_value(value)}")
    for warning in response.get("_hints", {}).get("warnings", []):
        lines.append(f"WARNING: {_value(warning)}")
    return "\n".join(lines)
