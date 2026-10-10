"""Connection normalization helpers for Tableau -> PBIP migration.

The helpers in this module intentionally keep only non-sensitive connection
metadata. They normalize source-specific fields into a stable shape that can
be written to ``connection_map.json`` and consumed by the PBIP generator.
"""

from __future__ import annotations

from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalise_source_type(source_type: Any, config: dict[str, Any]) -> str:
    raw = _clean(source_type or config.get("class") or config.get("connection_type") or config.get("source_type"))
    lowered = raw.lower()
    if lowered in {"sql server", "sqlserver", "mssql"}:
        return "sqlserver"
    if lowered in {"postgresql", "postgres", "postgresql_database"}:
        return "postgres"
    if lowered in {"snowflake"}:
        return "snowflake"
    return lowered or "unknown"


def extract_source_credentials(source_type: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized connection record for a source type/config pair.

    Supported source types:
    - Snowflake: account, warehouse, database, schema
    - SQL Server: server, database, authentication_type
    - Postgres: host, port, dbname

    Sensitive values such as passwords, tokens, and raw secrets are not
    propagated into the returned structure.
    """
    cfg = dict(config or {})
    normalized_type = _normalise_source_type(source_type, cfg)

    base: dict[str, Any] = {
        "source_type": normalized_type,
        "class": normalized_type,
        "name": _clean(cfg.get("name") or cfg.get("caption") or cfg.get("datasource") or cfg.get("table") or normalized_type),
        "tables": _clean(cfg.get("tables")),
        "table": _clean(cfg.get("table")),
        "schema": _clean(cfg.get("schema") or cfg.get("schema_name")),
        "database": _clean(cfg.get("database") or cfg.get("dbname")),
        "connection_mode": _clean(cfg.get("connection_mode")),
        "is_secure": True,
    }

    if normalized_type == "snowflake":
        account = _clean(cfg.get("account") or cfg.get("server") or cfg.get("host"))
        warehouse = _clean(cfg.get("warehouse"))
        database = _clean(cfg.get("database") or cfg.get("dbname"))
        schema = _clean(cfg.get("schema") or cfg.get("schema_name"))
        base.update(
            {
                "account": account,
                "server": account,
                "warehouse": warehouse,
                "database": database,
                "schema": schema,
                "connection_string": f'Snowflake.Databases("{account}", "{warehouse}")' if account and warehouse else "",
            }
        )
    elif normalized_type == "sqlserver":
        server = _clean(cfg.get("server") or cfg.get("host") or cfg.get("account"))
        database = _clean(cfg.get("database") or cfg.get("dbname"))
        authentication_type = _clean(
            cfg.get("authentication_type")
            or cfg.get("authentication")
            or cfg.get("authenticator")
            or cfg.get("integrated_security")
        )
        base.update(
            {
                "server": server,
                "database": database,
                "authentication_type": authentication_type,
                "connection_string": f'Sql.Database("{server}", "{database}")' if server and database else "",
            }
        )
    elif normalized_type == "postgres":
        host = _clean(cfg.get("host") or cfg.get("server"))
        port = _clean(cfg.get("port"))
        dbname = _clean(cfg.get("dbname") or cfg.get("database"))
        schema = _clean(cfg.get("schema") or cfg.get("schema_name") or "public")
        host_port = f"{host}:{port}" if port else host
        base.update(
            {
                "host": host,
                "port": port,
                "dbname": dbname,
                "database": dbname,
                "schema": schema,
                "connection_string": f'PostgreSQL.Database("{host_port}", "{dbname}")' if host and dbname else "",
            }
        )
    else:
        # Preserve only non-sensitive structural hints for unknown sources.
        for key in ("server", "host", "account", "warehouse", "port", "file_path", "query", "custom_sql", "dsn", "connection_mode"):
            value = _clean(cfg.get(key))
            if value:
                base[key] = value

    # Keep table hint if present so single-source models can be mapped later.
    for key in ("table", "tables"):
        value = _clean(cfg.get(key))
        if value:
            base[key] = value

    return {key: value for key, value in base.items() if value != ""}


def build_connection_artifacts(
    raw_connections: list[dict[str, Any]] | None,
    entity_names: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return sanitized connections plus a table→connection map.

    The table map is used by PBIP generation to inject the right M query into
    each table partition.
    """
    normalized_connections: list[dict[str, Any]] = []
    table_map: dict[str, dict[str, Any]] = {}

    raw_list = list(raw_connections or [])
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        normalized = extract_source_credentials(raw.get("class") or raw.get("source_type") or raw.get("connection_type"), raw)
        normalized_connections.append(normalized)

        tables_raw = _clean(raw.get("tables") or raw.get("table") or normalized.get("tables") or normalized.get("table"))
        tables = [item.strip() for item in tables_raw.split(",") if item.strip()] if tables_raw else []
        for table_name in tables:
            if table_name not in table_map:
                table_map[table_name] = dict(normalized, table=table_name)

    if entity_names and normalized_connections:
        if len(normalized_connections) == 1:
            sole = normalized_connections[0]
            for table_name in entity_names:
                if table_name and table_name not in table_map:
                    table_map[table_name] = dict(sole, table=table_name)

    return normalized_connections, table_map


__all__ = [
    "build_connection_artifacts",
    "extract_source_credentials",
]