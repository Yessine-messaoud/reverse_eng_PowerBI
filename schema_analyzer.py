from __future__ import annotations

from typing import Any


_NUMERIC_TYPES = {"int", "integer", "float", "double", "decimal", "number", "numeric", "real"}
_TIME_TYPES = {"date", "datetime", "timestamp", "time"}
_STRING_TYPES = {"string", "str", "text", "varchar", "char", "category"}


def _normalize_type(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "unknown"
    if any(token in value for token in _NUMERIC_TYPES):
        return "numeric"
    if any(token in value for token in _TIME_TYPES):
        return "time"
    if any(token in value for token in _STRING_TYPES):
        return "categorical"
    return "unknown"


def analyze_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Analyze a structured schema and classify fields."""
    tables = schema.get("tables", []) if isinstance(schema.get("tables"), list) else []
    measures: list[str] = []
    dimensions: list[str] = []
    time_fields: list[str] = []
    normalized_tables: list[dict[str, Any]] = []
    primary_table = ""

    for table in tables:
        if not isinstance(table, dict):
            continue
        table_name = str(table.get("name") or "").strip() or f"table_{len(normalized_tables) + 1}"
        source_name = str(table.get("source_name") or table.get("source") or "").strip()
        columns = table.get("columns", []) if isinstance(table.get("columns"), list) else []
        normalized_columns: list[dict[str, str]] = []
        score = 0

        for column in columns:
            if not isinstance(column, dict):
                continue
            name = str(column.get("name") or "").strip()
            if not name:
                continue
            normalized_type = _normalize_type(column.get("type"))
            normalized_columns.append({"name": name, "type": normalized_type})
            if normalized_type == "numeric":
                measures.append(name)
                score += 2
            elif normalized_type == "time":
                time_fields.append(name)
                dimensions.append(name)
                score += 1
            else:
                dimensions.append(name)

        normalized_tables.append({"name": table_name, "source_name": source_name, "columns": normalized_columns, "score": score})

    if normalized_tables:
        primary_table = max(normalized_tables, key=lambda item: (item["score"], len(item["columns"])))["name"]

    return {
        "measures": list(dict.fromkeys(measures)),
        "dimensions": list(dict.fromkeys(dimensions)),
        "time_fields": list(dict.fromkeys(time_fields)),
        "primary_table": primary_table,
        "tables": [{"name": table["name"], "source_name": table.get("source_name", ""), "columns": table["columns"]} for table in normalized_tables],
    }
