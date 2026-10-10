"""
Power Query M expression generator.

Produces valid, Power BI Desktop-compatible M connector expressions for every
major source type that Tableau workbooks can reference.  Each generated script
is a complete ``let … in`` block that Power BI Desktop will execute during
refresh (import mode) or use as the DirectQuery passthrough.

Supported source types
----------------------
Flat files    : csv, excel, json, parquet
Relational DB : sqlserver, postgres, mysql, oracle
Cloud DW      : snowflake, bigquery, redshift
Generic       : odbc, unknown (falls back to parameterised empty table)

Entry point
-----------
``generate_m_query(source_connection, table_name, columns)``
    Returns a ready-to-embed M script string.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.creation.pbix_generator import PBIXColumn


# ---------------------------------------------------------------------------
# Source-type normalisation
# ---------------------------------------------------------------------------

_CLASS_MAP: dict[str, str] = {
    # Flat files
    "csv":           "csv",
    "text":          "csv",
    "textscan":      "csv",
    "excel":         "excel",
    "excel-direct":  "excel",
    "xlsx":          "excel",
    "json":          "json",
    "parquet":       "parquet",
    # Relational
    "sqlserver":     "sqlserver",
    "mssql":         "sqlserver",
    "sql server":    "sqlserver",
    "postgres":      "postgres",
    "postgresql":    "postgres",
    "mysql":         "mysql",
    "oracle":        "oracle",
    "tableau_odbc_oracle": "oracle",
    # Cloud DW
    "snowflake":     "snowflake",
    "bigquery":      "bigquery",
    "bigquery-excel":"bigquery",
    "redshift":      "redshift",
    "amazon redshift":"redshift",
    # Generic
    "odbc":          "odbc",
    "genericodbc":   "odbc",
    "jdbc":          "odbc",
}


def _normalise_source_type(raw: str) -> str:
    return _CLASS_MAP.get(str(raw or "").strip().lower(), "unknown")


# ---------------------------------------------------------------------------
# Column type annotation helpers
# ---------------------------------------------------------------------------

_TMSL_TO_M_CAST: dict[str, str] = {
    "string":   "type text",
    "int64":    "Int64.Type",
    "double":   "type number",
    "decimal":  "Currency.Type",
    "boolean":  "type logical",
    "dateTime": "type datetime",
    "binary":   "type binary",
}


def _m_cast(tmsl_type: str) -> str:
    return _TMSL_TO_M_CAST.get(tmsl_type, "type text")


def _column_type_pairs(columns: "list[PBIXColumn]") -> str:
    """Build the M list literal used in Table.TransformColumnTypes."""
    if not columns:
        return ""
    pairs = ", ".join(
        f'{{"{_esc(c.name)}", {_m_cast(c.tmsl_type)}}}'
        for c in columns
    )
    return f"{{{pairs}}}"


def _esc(name: str) -> str:
    """Escape a value for embedding inside an M double-quoted string literal.

    In M, the only character that needs escaping inside "..." is a literal
    double-quote, which is doubled (""). Backslash has NO special meaning in
    M string literals — "localhost\\SQLEXPRESS" would be a WRONG escape.
    Windows named-instance notation (localhost\\SQLEXPRESS) must be preserved
    as a single backslash so the SQL connector receives the correct DSN.
    """
    return name.replace('"', '""')


# ---------------------------------------------------------------------------
# Schema-only fallback (used when no real connection info is available)
# ---------------------------------------------------------------------------

def _schema_only_partition(columns: "list[PBIXColumn]", table_name: str) -> str:
    """Empty typed table — schema inspectable, no rows, no network call."""
    from engine.creation.pbix_generator import _m_type, _safe_dax
    col_defs = ", ".join(
        f'"{_safe_dax(c.name)}" = {_m_type(c.tmsl_type)}'
        for c in columns
    ) or '"__placeholder__" = type text'
    return (
        f"let\n"
        f"    Source = #table(\n"
        f"        type table [{col_defs}],\n"
        f"        {{}}\n"
        f"    )\n"
        f"in\n"
        f"    Source"
    )


# ---------------------------------------------------------------------------
# Per-source M generators
# ---------------------------------------------------------------------------

def _gen_csv(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    file_path = (
        conn.get("file_path")
        or conn.get("path")
        or conn.get("filename")
        or conn.get("directory")
        or conn.get("file")
        or conn.get("filepath")
        or conn.get("dbname")
        or conn.get("server")
        or conn.get("host")
        or f"C:\\Data\\{table_name}.csv"
    )
    file_path = str(file_path)
    if not file_path.lower().endswith(".csv"):
        file_path = f"{file_path}.csv"
    # Normalise Windows paths — keep backslashes for M string literals
    file_path_m = str(file_path).replace('"', '""')
    n_cols = len(columns) if columns else 1
    type_pairs = _column_type_pairs(columns)
    delimiter = conn.get("delimiter") or conn.get("separator") or ","

    steps = [f'    Source = Csv.Document(File.Contents("{file_path_m}"),[Delimiter="{delimiter}", Columns={n_cols}, Encoding=65001, QuoteStyle=QuoteStyle.None])']
    steps.append('    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true])')
    if type_pairs:
        steps.append(f'    #"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers",{type_pairs})')
        last = '#"Changed Type"'
    else:
        last = '#"Promoted Headers"'
    return "let\n" + ",\n".join(steps) + f"\nin\n    {last}"


def _gen_excel(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    file_path = (
        conn.get("file_path")
        or conn.get("filename")
        or conn.get("dbname")
        or conn.get("server")
        or f"C:\\Data\\{table_name}.xlsx"
    )
    file_path_m = str(file_path).replace('"', '""')
    sheet = conn.get("sheet") or table_name or "Sheet1"
    type_pairs = _column_type_pairs(columns)

    steps = [f'    Source = Excel.Workbook(File.Contents("{file_path_m}"), null, true)']
    steps.append(f'    {_m_id(sheet)} = Source{{[Item="{_esc(sheet)}",Kind="Sheet"]}}[Data]')
    steps.append(f'    #"Promoted Headers" = Table.PromoteHeaders({_m_id(sheet)}, [PromoteAllScalars=true])')
    if type_pairs:
        steps.append(f'    #"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers",{type_pairs})')
        last = '#"Changed Type"'
    else:
        last = '#"Promoted Headers"'
    return "let\n" + ",\n".join(steps) + f"\nin\n    {last}"


def _gen_json(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    file_path = (
        conn.get("file_path")
        or conn.get("filename")
        or conn.get("dbname")
        or conn.get("server")
        or f"C:\\Data\\{table_name}.json"
    )
    file_path_m = str(file_path).replace('"', '""')
    return (
        f"let\n"
        f'    Source = Json.Document(File.Contents("{file_path_m}")),\n'
        f"    #\"Converted to Table\" = Table.FromList(Source, Splitter.SplitByNothing(), null, null, ExtraValues.Error),\n"
        f"    #\"Expanded Column\" = Table.ExpandRecordColumn(#\"Converted to Table\", \"Column1\", Table.ColumnNames(Table.FromRecords({{#\"Converted to Table\"{[Column1]}}}){{0}}))\n"
        f"in\n"
        f"    #\"Expanded Column\""
    )


def _gen_parquet(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    file_path = (
        conn.get("file_path")
        or conn.get("filename")
        or conn.get("dbname")
        or conn.get("server")
        or f"C:\\Data\\{table_name}.parquet"
    )
    file_path_m = str(file_path).replace('"', '""')
    return (
        f"let\n"
        f'    Source = Parquet.Document(File.Contents("{file_path_m}"))\n'
        f"in\n"
        f"    Source"
    )


def _gen_sqlserver(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    server = conn.get("server") or conn.get("host") or "localhost"
    database = conn.get("database") or conn.get("dbname") or "master"
    schema = conn.get("schema") or conn.get("schema_name") or "dbo"
    table = conn.get("table") or table_name

    # If there is a custom SQL query, use it directly
    custom_sql = conn.get("custom_sql") or conn.get("query") or ""
    if custom_sql:
        sql_esc = custom_sql.replace('"', '""')
        return (
            f"let\n"
            f'    Source = Sql.Database("{_esc(server)}", "{_esc(database)}"),\n'
            f'    Data = Value.NativeQuery(Source, "{sql_esc}", null, [EnableFolding=true])\n'
            f"in\n"
            f"    Data"
        )

    return (
        f"let\n"
        f'    Source = Sql.Database("{_esc(server)}", "{_esc(database)}"),\n'
        f'    dbo_{_safe_id(table)} = Source{{[Schema="{_esc(schema)}",Item="{_esc(table)}"]}}[Data]\n'
        f"in\n"
        f"    dbo_{_safe_id(table)}"
    )


def _gen_postgres(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    server = conn.get("server") or conn.get("host") or "localhost"
    port = conn.get("port") or "5432"
    database = conn.get("database") or conn.get("dbname") or "postgres"
    schema = conn.get("schema") or conn.get("schema_name") or "public"
    table = conn.get("table") or table_name
    host_port = f"{server}:{port}" if port else server

    custom_sql = conn.get("custom_sql") or conn.get("query") or ""
    if custom_sql:
        sql_esc = custom_sql.replace('"', '""')
        return (
            f"let\n"
            f'    Source = PostgreSQL.Database("{_esc(host_port)}", "{_esc(database)}"),\n'
            f'    Data = Value.NativeQuery(Source, "{sql_esc}", null, [EnableFolding=true])\n'
            f"in\n"
            f"    Data"
        )

    return (
        f"let\n"
        f'    Source = PostgreSQL.Database("{_esc(host_port)}", "{_esc(database)}"),\n'
        f'    {_safe_id(schema)}_{_safe_id(table)} = Source{{[Schema="{_esc(schema)}",Item="{_esc(table)}"]}}[Data]\n'
        f"in\n"
        f"    {_safe_id(schema)}_{_safe_id(table)}"
    )


def _gen_mysql(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    server = conn.get("server") or conn.get("host") or "localhost"
    port = conn.get("port") or "3306"
    database = conn.get("database") or conn.get("dbname") or "mysql"
    schema = conn.get("schema") or conn.get("schema_name") or database
    table = conn.get("table") or table_name
    host_port = f"{server}:{port}" if port else server

    return (
        f"let\n"
        f'    Source = MySql.Database("{_esc(host_port)}", "{_esc(database)}"),\n'
        f'    {_safe_id(schema)}_{_safe_id(table)} = Source{{[Schema="{_esc(schema)}",Item="{_esc(table)}"]}}[Data]\n'
        f"in\n"
        f"    {_safe_id(schema)}_{_safe_id(table)}"
    )


def _gen_oracle(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    server = conn.get("server") or conn.get("host") or "localhost"
    schema = (conn.get("schema") or conn.get("schema_name") or "").upper()
    table = (conn.get("table") or table_name).upper()

    return (
        f"let\n"
        f'    Source = Oracle.Database("{_esc(server)}", [HierarchicalNavigation=true]),\n'
        f'    {_safe_id(schema)}_{_safe_id(table)} = Source{{[Schema="{_esc(schema)}",Item="{_esc(table)}"]}}[Data]\n'
        f"in\n"
        f"    {_safe_id(schema)}_{_safe_id(table)}"
    )


def _gen_snowflake(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    server = conn.get("server") or conn.get("host") or conn.get("account") or "<account>.snowflakecomputing.com"
    warehouse = (conn.get("warehouse") or "COMPUTE_WH").upper()
    database = (conn.get("database") or conn.get("dbname") or "").upper()
    schema = (conn.get("schema") or conn.get("schema_name") or "PUBLIC").upper()
    table = (conn.get("table") or table_name).upper()

    # Ensure account suffix is present
    if not server.endswith(".snowflakecomputing.com"):
        server = f"{server}.snowflakecomputing.com"

    return (
        f"let\n"
        f'    Source = Snowflake.Databases("{_esc(server)}", "{_esc(warehouse)}"),\n'
        f'    Database = Source{{[Name="{_esc(database)}",Kind="Database"]}}[Data],\n'
        f'    Schema = Database{{[Name="{_esc(schema)}",Kind="Schema"]}}[Data],\n'
        f'    {_safe_id(table)} = Schema{{[Name="{_esc(table)}",Kind="Table"]}}[Data]\n'
        f"in\n"
        f"    {_safe_id(table)}"
    )


def _gen_bigquery(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    project = conn.get("database") or conn.get("project") or conn.get("server") or "<project>"
    dataset = conn.get("schema") or conn.get("schema_name") or conn.get("dataset") or "<dataset>"
    table = conn.get("table") or table_name

    return (
        f"let\n"
        f'    Source = GoogleBigQuery.Database([BillingProject="{_esc(project)}"]),\n'
        f'    {_safe_id(dataset)} = Source{{[Name="{_esc(dataset)}"]}}[Data],\n'
        f'    {_safe_id(table)} = {_safe_id(dataset)}{{[Name="{_esc(table)}"]}}[Data]\n'
        f"in\n"
        f"    {_safe_id(table)}"
    )


def _gen_redshift(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    server = conn.get("server") or conn.get("host") or "localhost"
    port = conn.get("port") or "5439"
    database = conn.get("database") or conn.get("dbname") or "dev"
    schema = conn.get("schema") or conn.get("schema_name") or "public"
    table = conn.get("table") or table_name
    host_port = f"{server}:{port}" if port else server

    return (
        f"let\n"
        f'    Source = AmazonRedshift.Database("{_esc(host_port)}", "{_esc(database)}"),\n'
        f'    {_safe_id(schema)}_{_safe_id(table)} = Source{{[Schema="{_esc(schema)}",Item="{_esc(table)}"]}}[Data]\n'
        f"in\n"
        f"    {_safe_id(schema)}_{_safe_id(table)}"
    )


def _gen_odbc(conn: dict[str, Any], table_name: str, columns: "list[PBIXColumn]") -> str:
    dsn = conn.get("dsn") or conn.get("server") or "<DSN>"
    connection_string = conn.get("connection_string") or conn.get("connect_string") or f"DSN={dsn}"
    query = conn.get("custom_sql") or conn.get("query") or f"SELECT * FROM {table_name}"
    conn_esc = connection_string.replace('"', '""')
    query_esc = query.replace('"', '""')

    return (
        f"let\n"
        f'    Source = Odbc.Query("{conn_esc}", "{query_esc}")\n'
        f"in\n"
        f"    Source"
    )


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------

def _safe_id(name: str) -> str:
    """Make a valid M identifier from an arbitrary string."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name or "x"))
    if s and s[0].isdigit():
        s = "_" + s
    return s or "x"


def _m_id(name: str) -> str:
    """Wrap identifier in #"..." notation if it contains special chars."""
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(name or "")):
        return str(name)
    return f'#"{_esc(name)}"'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

#: Dispatch table: normalised source type → generator function
_GENERATORS = {
    "csv":       _gen_csv,
    "excel":     _gen_excel,
    "json":      _gen_json,
    "parquet":   _gen_parquet,
    "sqlserver": _gen_sqlserver,
    "postgres":  _gen_postgres,
    "mysql":     _gen_mysql,
    "oracle":    _gen_oracle,
    "snowflake": _gen_snowflake,
    "bigquery":  _gen_bigquery,
    "redshift":  _gen_redshift,
    "odbc":      _gen_odbc,
}


def generate_m_query(
    source_connection: dict[str, Any],
    table_name: str,
    columns: "list[PBIXColumn] | None" = None,
) -> str:
    """Generate a Power Query M expression for *table_name* from *source_connection*.

    Parameters
    ----------
    source_connection:
        Dict with keys: ``class`` (Tableau connection class / source type),
        ``server``, ``database``, ``schema``, ``table``, ``file_path``,
        ``custom_sql``, ``warehouse``, ``port``, ``dsn``, etc.
        Unrecognised or missing fields default to sensible placeholders so
        the generated M is always syntactically valid.
    table_name:
        Fallback table name used when ``source_connection`` does not carry
        an explicit ``table`` key.
    columns:
        Optional list of ``PBIXColumn`` instances.  When provided, the
        generated M script includes a ``Table.TransformColumnTypes`` step
        that enforces the correct Power BI types.  When omitted, the script
        still connects to the source but skips type-casting.

    Returns
    -------
    str
        A complete ``let … in`` M script, syntactically valid and ready to
        embed in a TMSL partition's ``source.expression`` field.
    """
    cols = columns or []
    raw_type = (
        source_connection.get("class")
        or source_connection.get("connection_type")
        or source_connection.get("source_type")
        or "unknown"
    )
    normalised = _normalise_source_type(raw_type)
    gen_fn = _GENERATORS.get(normalised)

    if gen_fn is None:
        # Unknown source: fall through to schema-only placeholder
        return _schema_only_partition(cols, table_name)

    try:
        return gen_fn(source_connection, table_name, cols)
    except Exception:
        # Generation failed — still return valid M rather than crashing
        return _schema_only_partition(cols, table_name)


def describe_connection(source_connection: dict[str, Any]) -> str:
    """One-line human-readable summary of a source connection dict."""
    raw = source_connection.get("class") or source_connection.get("source_type") or "unknown"
    normalised = _normalise_source_type(raw)
    server = source_connection.get("server") or source_connection.get("host") or ""
    database = source_connection.get("database") or source_connection.get("dbname") or ""
    file_path = source_connection.get("file_path") or source_connection.get("filename") or ""
    if file_path:
        return f"{normalised}: {file_path}"
    if server and database:
        return f"{normalised}: {server}/{database}"
    if server:
        return f"{normalised}: {server}"
    return f"{normalised}: (no connection details)"
