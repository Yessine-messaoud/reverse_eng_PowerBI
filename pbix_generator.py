"""
Power BI Desktop file (.pbix) generator.

A .pbix is an Open Packaging Convention (OPC) ZIP archive whose internal
structure follows the Power BI Desktop specification:

    [Content_Types].xml         -- OPC content-type registry
    _rels/.rels                 -- root relationship graph
    DataModel                   -- inner ZIP: TMSL/Tabular model (schema + M scripts)
    Report/
        Layout                  -- JSON: page/visual definitions
        _rels/Layout.rels       -- XML: Layout relationships (OPC)
    DiagramLayout               -- JSON: data-model diagram node positions
    Metadata                    -- JSON: report creation/modification metadata
    SecurityBindings            -- JSON: row-level security bindings (empty by default)
    Settings                    -- JSON: report-level settings
    Version                     -- plain text: format version ("3.0")

DataModel inner ZIP is compatible with pbi-tools (open-source .pbix extractor).
It stores the tabular model as TMSL JSON (compatibilityLevel 1550) plus
per-table Power Query M partitions that describe the schema without embedding
actual binary data — making the generator purely Python with no Analysis
Services engine dependency.

Key classes
-----------
PBIXSemanticModel   Input data model (tables, measures, relationships)
PBIXGenerator       High-level entry point: model → .pbix file
PBIXPackager        Assembles all parts into the OPC ZIP
TMSLBuilder         Converts PBIXSemanticModel → TMSL JSON (DataModel)
PBIXLayoutBuilder   Converts PBIXSemanticModel → Report/Layout JSON
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------
# Desktop expects a package version string like "1.28" (UTF-16LE in Version part).
# Using "3.0" causes InvalidReportFile on recent Desktop builds.
_PBIX_FORMAT_VERSION = "1.28"
_COMPATIBILITY_LEVEL = 1601
_PBI_DESKTOP_VERSION = "2.148.578.0"
_REPORT_CONFIG_VERSION = "5.67"
_THEME_NAME = "CY25SU10"

# ---------------------------------------------------------------------------
# Data-type mapping tables
# ---------------------------------------------------------------------------

# SQL / generic → TMSL (Tabular Model) data types
_TMSL_TYPE: dict[str, str] = {
    "string": "string",
    "text": "string",
    "varchar": "string",
    "nvarchar": "string",
    "char": "string",
    "int": "int64",
    "integer": "int64",
    "bigint": "int64",
    "smallint": "int64",
    "tinyint": "int64",
    "float": "double",
    "double": "double",
    "real": "double",
    "decimal": "decimal",
    "numeric": "decimal",
    "money": "decimal",
    "boolean": "boolean",
    "bool": "boolean",
    "bit": "boolean",
    "date": "dateTime",
    "datetime": "dateTime",
    "datetime2": "dateTime",
    "timestamp": "dateTime",
    "time": "dateTime",
    "binary": "binary",
    "blob": "binary",
    "bytes": "binary",
}

# TMSL type → Power Query M type annotation (used in M partition scripts)
_M_TYPE: dict[str, str] = {
    "string": "type text",
    "int64": "type number",
    "double": "type number",
    "decimal": "type number",
    "boolean": "type logical",
    "dateTime": "type datetime",
    "binary": "type binary",
}

# TMSL type → default summarizeBy hint
_SUMMARIZE_BY: dict[str, str] = {
    "string": "none",
    "int64": "sum",
    "double": "sum",
    "decimal": "sum",
    "boolean": "none",
    "dateTime": "none",
    "binary": "none",
}

# DAX aggregation name → DAX function
_DAX_AGG: dict[str, str] = {
    "sum": "SUM",
    "avg": "AVERAGE",
    "average": "AVERAGE",
    "count": "COUNT",
    "countrows": "COUNTROWS",
    "min": "MIN",
    "max": "MAX",
    "distinctcount": "DISTINCTCOUNT",
}


def _tmsl_type(raw: str | None) -> str:
    return _TMSL_TYPE.get((raw or "").strip().lower(), "string")


def _m_type(tmsl: str) -> str:
    return _M_TYPE.get(tmsl, "type text")


def _summarize_by(tmsl: str) -> str:
    return _SUMMARIZE_BY.get(tmsl, "none")


def _dax_agg(name: str) -> str:
    return _DAX_AGG.get((name or "").strip().lower(), "SUM")


def _safe_dax(name: str) -> str:
    """Escape single-quotes in DAX identifiers."""
    return name.replace("'", "''")


def _sanitize_name(name: str) -> str:
    """Sanitize table/measure names for CSS selector compatibility.
    
    Replaces spaces and special characters with underscores to prevent
    JavaScript selector errors (spaces are invalid in CSS selectors).
    """
    # Replace spaces with underscores
    sanitized = str(name or "").replace(" ", "_")
    # Replace other problematic characters (but keep alphanumerics, underscores, hyphens)
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", sanitized)
    # Remove consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")
    return sanitized or "item"


# ---------------------------------------------------------------------------
# Public data model (input to the generator)
# ---------------------------------------------------------------------------

class PBICardinality(str, Enum):
    MANY_TO_ONE = "manyToOne"
    ONE_TO_ONE = "oneToOne"
    MANY_TO_MANY = "manyToMany"
    ONE_TO_MANY = "oneToMany"


class PBICrossFilter(str, Enum):
    SINGLE = "OneDirection"
    BOTH = "bothDirections"
    AUTOMATIC = "Automatic"


@dataclass
class PBIXColumn:
    name: str
    data_type: str                 # raw SQL / generic type string; mapped internally
    source_column: str = ""        # defaults to name when empty
    format_string: str = ""
    is_hidden: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not self.source_column:
            self.source_column = self.name

    @property
    def tmsl_type(self) -> str:
        return _tmsl_type(self.data_type)


@dataclass
class PBIXMeasure:
    name: str
    expression: str                # DAX expression
    format_string: str = ""
    description: str = ""
    display_folder: str = ""


@dataclass
class PBIXTable:
    name: str
    columns: list[PBIXColumn] = field(default_factory=list)
    measures: list[PBIXMeasure] = field(default_factory=list)
    is_date_table: bool = False
    description: str = ""
    # Optional: carries the Tableau/source connection dict so that
    # TMSLBuilder can generate a real Power Query M expression instead
    # of the schema-only placeholder.  Keys: class, server, database,
    # schema, table, file_path, custom_sql, warehouse, port, …
    source_connection: dict[str, Any] | None = None


@dataclass
class PBIXRelationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: PBICardinality = PBICardinality.MANY_TO_ONE
    cross_filter: PBICrossFilter = PBICrossFilter.SINGLE
    is_active: bool = True


@dataclass
class PBIXSemanticModel:
    name: str
    tables: list[PBIXTable] = field(default_factory=list)
    relationships: list[PBIXRelationship] = field(default_factory=list)
    culture: str = "en-US"
    description: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PBIXSemanticModel":
        """Construct from the JSON shape used throughout the pipeline.

        Expected shape (matches ai_control_backend semantic_schema output)::

            {
              "tables": [
                {
                  "name": "Sales",
                  "columns": [
                    {"name": "Date", "type": "date"},
                    {"name": "Amount", "type": "decimal"}
                  ],
                  "measures": [
                    {"name": "Total Sales", "expression": "SUM('Sales'[Amount])"}
                  ]
                }
              ],
              "relationships": [
                {"from_table": "Sales", "from_column": "DateKey",
                 "to_table": "Date", "to_column": "DateKey"}
              ]
            }
        """
        tables: list[PBIXTable] = []
        for tbl in payload.get("tables", []):
            columns = [
                PBIXColumn(
                    name=str(c.get("name") or c.get("column") or "Unknown"),
                    data_type=str(c.get("type") or c.get("dataType") or "string"),
                    format_string=str(c.get("formatString") or ""),
                    is_hidden=bool(c.get("isHidden") or c.get("is_hidden") or False),
                    description=str(c.get("description") or ""),
                )
                for c in tbl.get("columns", [])
            ]
            measures = [
                PBIXMeasure(
                    name=str(m.get("name") or "Measure"),
                    expression=str(m.get("expression") or m.get("dax") or "0"),
                    format_string=str(m.get("formatString") or m.get("format_string") or ""),
                    description=str(m.get("description") or ""),
                )
                for m in tbl.get("measures", [])
            ]
            raw_conn = tbl.get("source_connection")
            src_conn = raw_conn if isinstance(raw_conn, dict) and raw_conn else None
            tables.append(PBIXTable(
                name=str(tbl.get("name") or "Table"),
                columns=columns,
                measures=measures,
                description=str(tbl.get("description") or ""),
                source_connection=src_conn,
            ))

        relationships: list[PBIXRelationship] = []
        for rel in payload.get("relationships", []):
            try:
                cardinality = PBICardinality(rel.get("cardinality") or "manyToOne")
            except ValueError:
                cardinality = PBICardinality.MANY_TO_ONE
            cross_raw = str(
                rel.get("crossFilteringBehavior")
                or rel.get("cross_filter")
                or "OneDirection"
            ).strip()
            cross_norm = cross_raw.replace("_", "").replace(" ", "").lower()
            if cross_norm in {"bothdirections", "bothdirection"}:
                cross = PBICrossFilter.BOTH
            elif cross_norm in {"automatic"}:
                cross = PBICrossFilter.AUTOMATIC
            else:
                cross = PBICrossFilter.SINGLE
            relationships.append(PBIXRelationship(
                from_table=str(rel.get("from_table") or rel.get("fromTable") or ""),
                from_column=str(rel.get("from_column") or rel.get("fromColumn") or ""),
                to_table=str(rel.get("to_table") or rel.get("toTable") or ""),
                to_column=str(rel.get("to_column") or rel.get("toColumn") or ""),
                cardinality=cardinality,
                cross_filter=cross,
                is_active=bool(rel.get("isActive", True)),
            ))

        return cls(
            name=str(payload.get("name") or "Model"),
            tables=tables,
            relationships=relationships,
            culture=str(payload.get("culture") or "en-US"),
            description=str(payload.get("description") or ""),
        )


# ---------------------------------------------------------------------------
# TMSL (Tabular Model Scripting Language) builder
# ---------------------------------------------------------------------------

class TMSLBuilder:
    """Converts PBIXSemanticModel → TMSL JSON (compatibilityLevel 1550).

    The output is written as ``model.json`` inside the DataModel inner ZIP,
    which is the format used by pbi-tools for model serialisation.
    The TMSL schema is documented at:
    https://docs.microsoft.com/en-us/analysis-services/tmsl/tabular-model-scripting-language-tmsl-reference
    """

    def build(self, model: PBIXSemanticModel) -> dict[str, Any]:
        return {
            "compatibilityLevel": _COMPATIBILITY_LEVEL,
            "model": {
                "culture": model.culture,
                "defaultMode": "import",
                "defaultPowerBIDataSourceVersion": "powerBI_V3",
                "sourceQueryCulture": model.culture,
                "dataAccessOptions": {
                    "legacyRedirects": True,
                    "returnErrorValuesAsNull": True,
                },
                "discourageImplicitMeasures": True,
                "tables": [self._table(t) for t in model.tables],
                "relationships": [self._relationship(r) for r in model.relationships],
                "annotations": [
                    {"name": "PBIDesktopVersion", "value": _PBI_DESKTOP_VERSION},
                    {"name": "PBI_QueryOrder", "value": json.dumps([t.name for t in model.tables])},
                ],
            },
        }

    def _table(self, table: PBIXTable) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "name": table.name,
            "columns": [self._column(c, table.name) for c in table.columns],
            "partitions": [self._partition(table)],
        }
        if table.measures:
            obj["measures"] = [self._measure(m, table.name) for m in table.measures]
        if table.description:
            obj["description"] = table.description
        if table.is_date_table:
            obj["isDateTable"] = True
        return obj

    def _column(self, col: PBIXColumn, table_name: str) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "name": col.name,
            "dataType": col.tmsl_type,
            "sourceColumn": col.source_column,
            "summarizeBy": _summarize_by(col.tmsl_type),
            "lineageTag": _stable_id(f"{table_name}.{col.name}"),
            "annotations": [
                {"name": "SummarizationSetBy", "value": "Automatic"},
            ],
        }
        if col.format_string:
            obj["formatString"] = col.format_string
        if col.is_hidden:
            obj["isHidden"] = True
        if col.description:
            obj["description"] = col.description
        return obj

    def _measure(self, measure: PBIXMeasure, table_name: str) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "name": measure.name,
            "expression": measure.expression,
            "lineageTag": _stable_id(f"{table_name}.measure.{measure.name}"),
            "annotations": [
                {"name": "PBI_FormatHint", "value": "{\"isGeneralNumber\":true}"},
            ],
        }
        if measure.format_string:
            obj["formatString"] = measure.format_string
        if measure.description:
            obj["description"] = measure.description
        if measure.display_folder:
            obj["displayFolder"] = measure.display_folder
        return obj

    def _partition(self, table: PBIXTable) -> dict[str, Any]:
        """Generate a Power Query M partition for *table*.

        When *table.source_connection* is populated the partition uses a real
        connector expression (CSV, SQL Server, PostgreSQL, Snowflake, …)
        generated by ``m_query_generator.generate_m_query``.

        When no connection info is available the partition falls back to an
        empty typed table so that the schema remains inspectable in Power BI
        Desktop without requiring a live data source.
        """
        if table.source_connection:
            try:
                from engine.creation.m_query_generator import generate_m_query
                m_script = generate_m_query(
                    table.source_connection,
                    table.name,
                    table.columns,
                )
                _log.debug(
                    "partition_m_real: table=%s source_type=%s",
                    table.name,
                    table.source_connection.get("class") or table.source_connection.get("source_type"),
                )
            except Exception as exc:
                _log.warning("m_query_gen failed for %s: %s — using schema-only fallback", table.name, exc)
                m_script = self._schema_only_m(table)
        else:
            m_script = self._schema_only_m(table)

        return {
            "name": "Partition",
            "mode": "import",
            "source": {
                "type": "m",
                "expression": m_script,
            },
        }

    def _schema_only_m(self, table: PBIXTable) -> str:
        """Empty typed table — schema-only, no data source required."""
        col_defs = ", ".join(
            f'"{_safe_dax(c.name)}" = {_m_type(c.tmsl_type)}'
            for c in table.columns
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

    def _relationship(self, rel: PBIXRelationship) -> dict[str, Any]:
        name = f"{rel.from_table}_{rel.from_column}_{rel.to_table}_{rel.to_column}"
        return {
            "name": name,
            "fromTable": rel.from_table,
            "fromColumn": rel.from_column,
            "toTable": rel.to_table,
            "toColumn": rel.to_column,
            "fromCardinality": rel.cardinality.value.replace("To", "->").split("->")[0].lower(),
            "toCardinality": rel.cardinality.value.replace("To", "->").split("->")[1].lower(),
            "isActive": rel.is_active,
            "crossFilteringBehavior": rel.cross_filter.value,
        }


# ---------------------------------------------------------------------------
# Report/Layout builder
# ---------------------------------------------------------------------------

class PBIXLayoutBuilder:
    """Generates the Report/Layout JSON.

    Produces one page per table with a basic bar-chart visual so the report
    opens in Power BI Desktop with visible content.  The layout JSON schema
    is documented in:
    https://github.com/microsoft/powerbi-visuals-tools/blob/master/schema/schema.layout.json
    """

    _PAGE_W = 1280
    _PAGE_H = 720

    def build(self, model: PBIXSemanticModel) -> dict[str, Any]:
        sections = []
        for idx, table in enumerate(model.tables):
            sections.append(self._section(idx, table, model.name))
        if not sections:
            sections = [self._empty_section()]
        config = {
            "version": _REPORT_CONFIG_VERSION,
            "themeCollection": {
                "baseTheme": {
                    "name": _THEME_NAME,
                    "version": _REPORT_CONFIG_VERSION,
                    "type": 2,
                }
            },
        }
        return {
            "id": 0,
            "resourcePackages": [],
            "sections": sections,
            "config": json.dumps(config, separators=(",", ":")),
            "filters": "[]",
        }

    def _section(self, ordinal: int, table: PBIXTable, model_name: str) -> dict[str, Any]:
        visuals = self._visuals_for_table(table)
        section_config = {"defaultFilters": [], "visibility": 0}
        return {
            "id": ordinal,
            "name": f"Section{ordinal + 1}",
            "displayName": table.name,
            "filters": "[]",
            "ordinal": ordinal,
            "visualContainers": visuals,
            "config": json.dumps(section_config, separators=(",", ":")),
            "width": self._PAGE_W,
            "height": self._PAGE_H,
            "displayOption": 0,
        }

    def _empty_section(self) -> dict[str, Any]:
        return {
            "id": 0,
            "name": "Section1",
            "displayName": "Page 1",
            "filters": "[]",
            "ordinal": 0,
            "visualContainers": [],
            "config": json.dumps({"defaultFilters": [], "visibility": 0}, separators=(",", ":")),
            "width": self._PAGE_W,
            "height": self._PAGE_H,
            "displayOption": 0,
        }

    def _visuals_for_table(self, table: PBIXTable) -> list[dict[str, Any]]:
        """Create one chart visual + one card visual per measure."""
        visuals: list[dict[str, Any]] = []

        # Pick a categorical column (prefer string/date) and a numeric column
        cat_col = next(
            (c for c in table.columns if c.tmsl_type in ("string", "dateTime")),
            table.columns[0] if table.columns else None,
        )
        num_col = next(
            (c for c in table.columns if c.tmsl_type in ("int64", "double", "decimal")),
            None,
        )

        # Bar chart: categorical × numeric
        if cat_col and num_col:
            visuals.append(self._bar_chart(table, cat_col, num_col, x=20, y=20))

        # Cards for each explicit measure
        for m_idx, measure in enumerate(table.measures[:4]):
            card_x = 20 + m_idx * 220
            visuals.append(self._card(table, measure, x=card_x, y=450))

        # Table visual if no chart (no suitable columns)
        if not visuals and table.columns:
            visuals.append(self._table_visual(table, x=20, y=20))

        return visuals

    def _bar_chart(
        self,
        table: PBIXTable,
        cat: PBIXColumn,
        num: PBIXColumn,
        x: int,
        y: int,
    ) -> dict[str, Any]:
        cat_ref = f"{table.name}.{cat.name}"
        num_ref = f"{table.name}.{num.name}"

        query = {
            "Version": 2,
            "From": [{"Name": "t", "Entity": table.name, "Type": 0}],
            "Select": [
                {
                    "Column": {
                        "Expression": {"SourceRef": {"Source": "t"}},
                        "Property": cat.name,
                    },
                    "Name": cat_ref,
                },
                {
                    "Aggregation": {
                        "Expression": {
                            "Column": {
                                "Expression": {"SourceRef": {"Source": "t"}},
                                "Property": num.name,
                            }
                        },
                        "Function": 0,
                    },
                    "Name": num_ref,
                },
            ],
            "OrderBy": [],
        }
        vis_config = {
            "name": f"clusteredBarChart_{_sanitize_name(table.name)}",
            "layouts": [
                {
                    "id": 0,
                    "position": {"x": x, "y": y, "z": 0, "width": 600, "height": 380, "tabOrder": 0},
                }
            ],
            "singleVisual": {
                "visualType": "clusteredBarChart",
                "projections": {
                    "Y": [{"queryRef": num_ref, "active": True}],
                    "Category": [{"queryRef": cat_ref}],
                },
                "prototypeQuery": query,
                "columnProperties": {
                    num_ref: {"displayName": f"Sum of {num.name}"},
                    cat_ref: {"displayName": cat.name},
                },
            },
        }
        transforms = {
            "queryMetadata": {
                "Selects": [
                    {"Name": num_ref, "Restatement": f"Sum of {num.name}", "Type": 260},
                    {"Name": cat_ref, "Restatement": cat.name, "Type": 1},
                ]
            },
            "roleManipulations": {
                "Y": [{"queryRef": num_ref, "active": True}],
                "Category": [{"queryRef": cat_ref, "active": True}],
            },
        }
        return {
            "x": x,
            "y": y,
            "z": 0,
            "width": 600,
            "height": 380,
            "config": json.dumps(vis_config, separators=(",", ":")),
            "filters": "[]",
            "query": json.dumps(query, separators=(",", ":")),
            "dataTransforms": json.dumps(transforms, separators=(",", ":")),
        }

    def _card(
        self,
        table: PBIXTable,
        measure: PBIXMeasure,
        x: int,
        y: int,
    ) -> dict[str, Any]:
        measure_ref = f"{table.name}.{measure.name}"
        query = {
            "Version": 2,
            "From": [{"Name": "t", "Entity": table.name, "Type": 0}],
            "Select": [
                {
                    "Measure": {
                        "Expression": {"SourceRef": {"Source": "t"}},
                        "Property": measure.name,
                    },
                    "Name": measure_ref,
                }
            ],
        }
        vis_config = {
            "name": f"card_{_sanitize_name(table.name)}_{_sanitize_name(measure.name)}",
            "layouts": [
                {"id": 0, "position": {"x": x, "y": y, "z": 0, "width": 200, "height": 100, "tabOrder": 100}}
            ],
            "singleVisual": {
                "visualType": "card",
                "projections": {"Values": [{"queryRef": measure_ref, "active": True}]},
                "prototypeQuery": query,
                "columnProperties": {measure_ref: {"displayName": measure.name}},
            },
        }
        transforms = {
            "queryMetadata": {
                "Selects": [{"Name": measure_ref, "Restatement": measure.name, "Type": 260}]
            },
            "roleManipulations": {"Values": [{"queryRef": measure_ref, "active": True}]},
        }
        return {
            "x": x,
            "y": y,
            "z": 0,
            "width": 200,
            "height": 100,
            "config": json.dumps(vis_config, separators=(",", ":")),
            "filters": "[]",
            "query": json.dumps(query, separators=(",", ":")),
            "dataTransforms": json.dumps(transforms, separators=(",", ":")),
        }

    def _table_visual(self, table: PBIXTable, x: int, y: int) -> dict[str, Any]:
        selects = [
            {
                "Column": {
                    "Expression": {"SourceRef": {"Source": "t"}},
                    "Property": c.name,
                },
                "Name": f"{table.name}.{c.name}",
            }
            for c in table.columns[:10]
        ]
        query = {
            "Version": 2,
            "From": [{"Name": "t", "Entity": table.name, "Type": 0}],
            "Select": selects,
        }
        vis_config = {
            "name": f"table_{_sanitize_name(table.name)}",
            "layouts": [
                {"id": 0, "position": {"x": x, "y": y, "z": 0, "width": 800, "height": 400, "tabOrder": 0}}
            ],
            "singleVisual": {
                "visualType": "tableEx",
                "projections": {
                    "Values": [{"queryRef": f"{table.name}.{c.name}"} for c in table.columns[:10]]
                },
                "prototypeQuery": query,
            },
        }
        return {
            "x": x,
            "y": y,
            "z": 0,
            "width": 800,
            "height": 400,
            "config": json.dumps(vis_config, separators=(",", ":")),
            "filters": "[]",
            "query": json.dumps(query, separators=(",", ":")),
            "dataTransforms": "{}",
        }


# ---------------------------------------------------------------------------
# DiagramLayout builder
# ---------------------------------------------------------------------------

class PBIXDiagramLayoutBuilder:
    """Generates DiagramLayout JSON (data model diagram node positions)."""

    _NODE_W = 180
    _NODE_H = 120
    _GAP_X = 240
    _GAP_Y = 180

    def build(self, model: PBIXSemanticModel) -> dict[str, Any]:
        nodes = []
        cols = max(1, int(len(model.tables) ** 0.5) + 1)
        for idx, table in enumerate(model.tables):
            row, col = divmod(idx, cols)
            nodes.append({
                "nodeIndex": table.name,
                "size": {"width": self._NODE_W, "height": self._NODE_H},
                "location": {
                    "x": col * self._GAP_X + 40,
                    "y": row * self._GAP_Y + 40,
                },
            })
        return {
            "version": "1.1.0",
            "diagrams": [
                {
                    "ordinal": 0,
                    "nodes": nodes,
                    "zoomValue": 100,
                    "scrollPosition": {"x": 0, "y": 0},
                }
            ],
        }


# ---------------------------------------------------------------------------
# Metadata / Settings builders
# ---------------------------------------------------------------------------

def _build_metadata(model: PBIXSemanticModel) -> dict[str, Any]:
    return {
        "Version": 5,
        "AutoCreatedRelationships": [],
        "CreatedFrom": "Cloud",
        "CreatedFromRelease": "2026.04",
    }


def _build_settings() -> dict[str, Any]:
    return {
        "Version": 4,
        "ReportSettings": {},
        "QueriesSettings": {
            "TypeDetectionEnabled": True,
            "RelationshipImportEnabled": True,
            "Version": _PBI_DESKTOP_VERSION,
        },
    }


def _pbix_text_bytes(text: str) -> bytes:
    """Encode PBIX textual parts in UTF-16LE expected by Desktop packager."""
    return text.encode("utf-16le")


# ---------------------------------------------------------------------------
# OPC (Open Packaging Convention) XML builders
# ---------------------------------------------------------------------------

def _content_types_xml(has_security_bindings: bool = True) -> str:
    overrides = [
        ('"/DataModel"', "application/vnd.ms-pkidiscovery.pbi.DataModel+zip"),
        ('"/Report/Layout"', "application/json"),
        ('"/DiagramLayout"', "application/json"),
        ('"/Metadata"', "application/json"),
        ('"/Settings"', "application/json"),
        ('"/Version"', "application/vnd.ms-powerbi.report.version"),
    ]
    if has_security_bindings:
        overrides.append(('"/SecurityBindings"', "application/json"))
    parts = "\n".join(
        f'  <Override PartName={pn} ContentType="{ct}"/>'
        for pn, ct in overrides
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.ms-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        f"{parts}\n"
        "</Types>"
    )


def _root_rels_xml() -> str:
    rels = [
        ("rId1", "http://schemas.microsoft.com/package/2012/relationships/datamodel", "DataModel"),
        ("rId2", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", "Report/Layout"),
        ("rId3", "http://schemas.microsoft.com/powerbi/2016/relationships/metadata", "Metadata"),
        ("rId4", "http://schemas.microsoft.com/powerbi/2016/relationships/settings", "Settings"),
        ("rId5", "http://schemas.microsoft.com/powerbi/2016/relationships/version", "Version"),
        ("rId6", "http://schemas.microsoft.com/powerbi/2016/relationships/diagramlayout", "DiagramLayout"),
        ("rId7", "http://schemas.microsoft.com/powerbi/2016/relationships/securitybindings", "SecurityBindings"),
    ]
    entries = "\n".join(
        f'  <Relationship Id="{rid}" Type="{typ}" Target="{tgt}"/>'
        for rid, typ, tgt in rels
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f"{entries}\n"
        "</Relationships>"
    )


def _report_layout_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        "</Relationships>"
    )


# ---------------------------------------------------------------------------
# DataModel inner ZIP builder
# ---------------------------------------------------------------------------

def _build_data_model_bytes(model: PBIXSemanticModel) -> bytes:
    """Build the DataModel inner ZIP in-memory.

    Structure (pbi-tools compatible)::

        DataModel (inner ZIP, DEFLATED)
        ├── model.json          TMSL database definition (compatibilityLevel 1601)
        └── database.json       Lightweight alias for tooling compatibility

    Both files contain the same TMSL JSON.  Real Power BI Desktop files contain
    a binary Vertipaq (xVelocity) database dump; this generator produces the
    schema-only TMSL representation which is:
    - Readable by pbi-tools and Tabular Editor
    - Uploadable to Power BI Service via the XMLA endpoint
    - Openable in Power BI Desktop (as an import-mode dataset with no rows)
    """
    tmsl = TMSLBuilder().build(model)
    tmsl_bytes = json.dumps(tmsl, indent=2, ensure_ascii=False).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("model.json", tmsl_bytes)
        zf.writestr("database.json", tmsl_bytes)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main packager
# ---------------------------------------------------------------------------

class PBIXPackager:
    """Assembles all parts into a valid OPC .pbix ZIP archive.

    The outer ZIP uses ZIP_STORED (no compression) for most parts, which is
    consistent with how Power BI Desktop writes .pbix files.  The DataModel
    inner ZIP is DEFLATED internally and stored as-is in the outer archive.
    """

    def pack(
        self,
        model: PBIXSemanticModel,
        output: Path | None = None,
        *,
        layout_override: dict[str, Any] | None = None,
        template_pbix: Path | None = None,
    ) -> bytes:
        """Pack *model* into a .pbix byte stream.

        Parameters
        ----------
        model:
            The semantic model to package.
        output:
            If given, write the bytes to this path in addition to returning them.

        Returns
        -------
        bytes
            Raw .pbix archive content.
        """
        layout_builder = PBIXLayoutBuilder()
        diagram_builder = PBIXDiagramLayoutBuilder()

        layout_payload = layout_override if layout_override is not None else layout_builder.build(model)
        layout_json = json.dumps(layout_payload, indent=2, ensure_ascii=False)
        diagram_json = json.dumps(diagram_builder.build(model), indent=2, ensure_ascii=False)
        metadata_json = json.dumps(_build_metadata(model), indent=2, ensure_ascii=False)
        settings_json = json.dumps(_build_settings(), indent=2, ensure_ascii=False)
        security_json = "[]"

        buf = io.BytesIO()
        if template_pbix is not None and template_pbix.exists():
            # ── Template path ─────────────────────────────────────────────────
            # Copy the ABF DataModel bytes VERBATIM from the template.
            # Power BI Desktop requires the DataModel to be in ABF (Analysis
            # Services Backup File) binary format.  The pure-Python ZIP-in-ZIP
            # generated by _build_data_model_bytes() is rejected with
            # PFE_UNABLE_VERIFY_ABF_HEADER.  By preserving the template ABF
            # byte-for-byte we guarantee the file opens in Power BI Desktop.
            with zipfile.ZipFile(template_pbix, "r") as src:
                template_dm_bytes = src.read("DataModel")
                template_ct_xml = src.read("[Content_Types].xml") if "[Content_Types].xml" in src.namelist() else None
                template_rels_xml = src.read("_rels/.rels") if "_rels/.rels" in src.namelist() else None

            _log.info(
                "template_datamodel: preserved %d bytes (ABF) from %s",
                len(template_dm_bytes),
                template_pbix.name,
            )

            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                # Use template OPC metadata to ensure relationship graph is consistent
                zf.writestr(
                    "[Content_Types].xml",
                    template_ct_xml.decode("utf-8") if template_ct_xml else _content_types_xml(),
                )
                zf.writestr(
                    "_rels/.rels",
                    template_rels_xml.decode("utf-8") if template_rels_xml else _root_rels_xml(),
                )
                # DataModel: ABF binary copied byte-for-byte (DO NOT regenerate)
                zf.writestr(zipfile.ZipInfo("DataModel"), template_dm_bytes)
                # Generated parts: layout, diagram, metadata
                zf.writestr("Report/Layout", _pbix_text_bytes(layout_json))
                zf.writestr("Report/_rels/Layout.rels", _report_layout_rels_xml())
                zf.writestr("DiagramLayout", _pbix_text_bytes(diagram_json))
                zf.writestr("Metadata", _pbix_text_bytes(metadata_json))
                zf.writestr("SecurityBindings", _pbix_text_bytes(security_json))
                zf.writestr("Settings", _pbix_text_bytes(settings_json))
                zf.writestr("Version", _pbix_text_bytes(_PBIX_FORMAT_VERSION))
        else:
            # ── Scratch path (no template available) ──────────────────────────
            # WARNING: This produces a ZIP-in-ZIP DataModel that Power BI Desktop
            # will reject with PFE_UNABLE_VERIFY_ABF_HEADER.  Use only for
            # structural validation, pbi-tools workflows, or XMLA endpoint upload.
            _log.warning(
                "No template PBIX found. Falling back to ZIP-in-ZIP DataModel. "
                "Generated PBIX will NOT open in Power BI Desktop."
            )
            data_model_bytes = _build_data_model_bytes(model)
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr("[Content_Types].xml", _content_types_xml())
                zf.writestr("_rels/.rels", _root_rels_xml())
                zf.writestr(zipfile.ZipInfo("DataModel"), data_model_bytes)
                zf.writestr("Report/Layout", _pbix_text_bytes(layout_json))
                zf.writestr("Report/_rels/Layout.rels", _report_layout_rels_xml())
                zf.writestr("DiagramLayout", _pbix_text_bytes(diagram_json))
                zf.writestr("Metadata", _pbix_text_bytes(metadata_json))
                zf.writestr("SecurityBindings", _pbix_text_bytes(security_json))
                zf.writestr("Settings", _pbix_text_bytes(settings_json))
                zf.writestr("Version", _pbix_text_bytes(_PBIX_FORMAT_VERSION))

        payload = buf.getvalue()
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
            _log.info("pbix written: path=%s size=%d bytes", output, len(payload))
        return payload


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

_REQUIRED_PARTS_BASE = frozenset({
    "[Content_Types].xml",
    "DataModel",
    "Report/Layout",
    "DiagramLayout",
    "Metadata",
    "Settings",
    "Version",
})


@dataclass
class PBIXVerificationResult:
    path: str
    size_bytes: int
    sha256: str
    parts_found: list[str]
    parts_missing: list[str]
    inner_model_tables: list[str]
    is_structurally_valid: bool
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "parts_found": self.parts_found,
            "parts_missing": self.parts_missing,
            "inner_model_tables": self.inner_model_tables,
            "is_structurally_valid": self.is_structurally_valid,
            "warnings": self.warnings,
        }


def verify_pbix(path: Path) -> PBIXVerificationResult:
    """Structural integrity check for a generated .pbix file.

    Checks:
    - Outer ZIP is a valid ZIP archive
    - All required OPC parts are present
    - DataModel inner ZIP is readable
    - TMSL model.json parses correctly and lists expected tables
    """
    warnings: list[str] = []
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    size = len(raw)

    try:
        outer = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        return PBIXVerificationResult(
            path=str(path),
            size_bytes=size,
            sha256=sha256,
            parts_found=[],
            parts_missing=list(_REQUIRED_PARTS_BASE),
            inner_model_tables=[],
            is_structurally_valid=False,
            warnings=[f"Outer ZIP is corrupt: {exc}"],
        )

    names = set(outer.namelist())
    parts_found = sorted(n for n in names if n in _REQUIRED_PARTS_BASE or n == "_rels/.rels")
    parts_missing = sorted(_REQUIRED_PARTS_BASE - names)

    # ABF binary DataModel signature: UTF-16LE "This backup was..."
    _ABF_MAGIC = b"\x54\x00\x68\x00\x69\x00\x73\x00"

    inner_tables: list[str] = []
    if "DataModel" in names:
        try:
            inner_bytes = outer.read("DataModel")
            if inner_bytes[:8].startswith(_ABF_MAGIC):
                # ABF binary (valid Power BI Desktop format — copied from real .pbix template)
                preview = inner_bytes[:32].decode("utf-16le", errors="replace")
                warnings.append(
                    f"DataModel is ABF binary ({len(inner_bytes):,} bytes) — "
                    f"valid for Power BI Desktop. Preview: {preview!r}"
                )
            elif inner_bytes[:4] == b"\x50\x4b\x03\x04":
                # ZIP-in-ZIP: the old generated format rejected by Power BI Desktop
                try:
                    inner_zip = zipfile.ZipFile(io.BytesIO(inner_bytes))
                    model_names = inner_zip.namelist()
                    for candidate in ("model.json", "database.json"):
                        if candidate in model_names:
                            tmsl = json.loads(inner_zip.read(candidate))
                            inner_tables = [t["name"] for t in tmsl.get("model", {}).get("tables", [])]
                            break
                    warnings.append(
                        "DataModel is ZIP-in-ZIP (TMSL JSON). Power BI Desktop will reject "
                        "this file with PFE_UNABLE_VERIFY_ABF_HEADER. Use a .pbix template "
                        "to provide a valid ABF DataModel."
                    )
                except Exception as exc:
                    warnings.append(f"DataModel inner ZIP parse failed: {exc}")
            else:
                warnings.append(
                    f"DataModel format unknown (header={inner_bytes[:8].hex()}). "
                    "May not open in Power BI Desktop."
                )
        except Exception as exc:
            warnings.append(f"DataModel read failed: {exc}")
    else:
        warnings.append("DataModel part absent — file will not open in Power BI Desktop")

    def _decode_json_part(raw_bytes: bytes) -> Any:
        for enc in ("utf-8", "utf-16le"):
            try:
                return json.loads(raw_bytes.decode(enc))
            except Exception:
                continue
        return json.loads(raw_bytes)

    if "Report/Layout" in names:
        try:
            _decode_json_part(outer.read("Report/Layout"))
        except Exception as exc:
            warnings.append(f"Report/Layout is not valid JSON: {exc}")

    return PBIXVerificationResult(
        path=str(path),
        size_bytes=size,
        sha256=sha256,
        parts_found=parts_found,
        parts_missing=parts_missing,
        inner_model_tables=inner_tables,
        is_structurally_valid=len(parts_missing) == 0,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

class PBIXGenerator:
    """High-level API: semantic model dict → .pbix file.

    Usage::

        model = PBIXSemanticModel.from_dict(semantic_schema)
        generator = PBIXGenerator()
        result = generator.generate(model, Path("output/report.pbix"))
        print(result["sha256"])
    """

    def __init__(self) -> None:
        self._packager = PBIXPackager()
        # Prefer the smallest working PBIX (mstest < SalesReport) to minimise I/O.
        # Paths are resolved relative to the current working directory so that the
        # generator works whether called from backend/ or its parent.
        _here = Path(__file__).resolve().parent
        _backend = _here.parent.parent  # engine/creation → repo root
        self._template_candidates = [
            _backend / "input" / "mstest.pbix",
            _backend / "input" / "SalesReport.pbix",
            Path("input/mstest.pbix"),
            Path("input/SalesReport.pbix"),
            Path("backend/input/mstest.pbix"),
            Path("backend/input/SalesReport.pbix"),
        ]

    def _template_pbix(self) -> Path | None:
        # Power BI Desktop requires the DataModel to be in ABF (Analysis Services
        # Backup File) binary format.  Pure-Python TMSL JSON wrapped in an inner
        # ZIP is rejected with PFE_UNABLE_VERIFY_ABF_HEADER.
        # Solution: copy the DataModel verbatim from a known-good .pbix template
        # that was originally saved by Power BI Desktop.
        for p in self._template_candidates:
            if p.exists() and p.stat().st_size > 1_000:
                _log.info("Using PBIX template for ABF DataModel: %s", p)
                return p
        _log.warning(
            "No PBIX template found. Generated DataModel will be ZIP-in-ZIP which "
            "Power BI Desktop rejects with PFE_UNABLE_VERIFY_ABF_HEADER. "
            "Place a working .pbix in input/mstest.pbix or input/SalesReport.pbix."
        )
        return None

    def generate(
        self,
        model: PBIXSemanticModel | dict[str, Any],
        output_path: Path,
        *,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Generate a .pbix file from *model*.

        Parameters
        ----------
        model:
            Either a PBIXSemanticModel instance or a raw dict (passed through
            PBIXSemanticModel.from_dict).
        output_path:
            Destination file path.  Parent directories are created if absent.
        verify:
            When True (default) run structural verification after writing.

        Returns
        -------
        dict
            ``{"ok": bool, "path": str, "size_bytes": int, "sha256": str,
               "verification": {...}}``
        """
        if isinstance(model, dict):
            model = PBIXSemanticModel.from_dict(model)

        _log.info(
            "pbix_generate: model=%s tables=%d relationships=%d",
            model.name,
            len(model.tables),
            len(model.relationships),
        )

        payload = self._packager.pack(model, output=output_path, template_pbix=self._template_pbix())

        result: dict[str, Any] = {
            "ok": True,
            "path": str(output_path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

        if verify:
            vr = verify_pbix(output_path)
            result["verification"] = vr.as_dict()
            if not vr.is_structurally_valid:
                result["ok"] = False
                _log.error("pbix verification failed: missing=%s", vr.parts_missing)
            else:
                _log.info("pbix verification passed: tables=%s", vr.inner_model_tables)

        return result

    def generate_from_abstract_spec(
        self,
        abstract_spec: dict[str, Any],
        output_path: Path,
        *,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Generate a .pbix from AbstractSpec (dashboard_spec + semantic_model + data_lineage).

        Uses ``PBIXReportBuilder`` to produce a fully faithful report layout with:
        - One page per Tableau dashboard
        - All visual types (bar, line, pie, scatter, table, card, map, treemap, …)
        - Correct field bindings from the semantic model
        - Slicer containers for filters
        - Source color palette applied to visuals
        """
        model = self._model_from_abstract_spec(abstract_spec)
        layout = self._layout_from_abstract_spec(abstract_spec, model)
        payload = self._packager.pack(
            model,
            output=output_path,
            layout_override=layout,
            template_pbix=self._template_pbix(),
        )

        result: dict[str, Any] = {
            "ok": True,
            "path": str(output_path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "audit": {
                "relationships_count": len(model.relationships),
                "tables_count": len(model.tables),
            },
        }
        if verify:
            vr = verify_pbix(output_path)
            result["verification"] = vr.as_dict()
            result["ok"] = vr.is_structurally_valid
        return result

    def _model_from_abstract_spec(self, spec: dict[str, Any]) -> PBIXSemanticModel:
        semantic = spec.get("semantic_model", {}) if isinstance(spec.get("semantic_model"), dict) else {}
        entities = semantic.get("entities", []) if isinstance(semantic.get("entities"), list) else []
        measures = semantic.get("measures", []) if isinstance(semantic.get("measures"), list) else []
        rels = semantic.get("relationships", []) if isinstance(semantic.get("relationships"), list) else []

        measure_names = {str(m.get("name")) for m in measures if isinstance(m, dict) and m.get("name")}
        fact_table = str(semantic.get("fact_table", "") or "")

        # Group measures by their source table for assignment below.
        measures_by_table: dict[str, list[PBIXMeasure]] = {}
        for m in measures:
            if not isinstance(m, dict):
                continue
            m_name = str(m.get("name") or "Measure").strip()
            expr = str(m.get("expression") or m.get("dax") or "0").strip()
            src_cols: list[Any] = m.get("source_columns", [])
            target = fact_table
            if src_cols and isinstance(src_cols[0], dict):
                target = str(src_cols[0].get("table", "") or fact_table or "").strip()
            if not target:
                target = fact_table or "__measures__"
            measures_by_table.setdefault(target, []).append(
                PBIXMeasure(
                    name=m_name,
                    expression=expr,
                    format_string=str(m.get("format_string") or ""),
                    description=str(m.get("description") or ""),
                )
            )

        tables: list[PBIXTable] = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            t_name = str(entity.get("name") or entity.get("id") or "Table")
            cols: list[PBIXColumn] = []
            for c in entity.get("columns", []) if isinstance(entity.get("columns"), list) else []:
                if not isinstance(c, dict):
                    continue
                c_name = str(c.get("name") or "Column")
                role = str(c.get("role") or "").lower()
                raw = str(c.get("pbi_type") or c.get("type") or "string")
                # Healing: force numeric type when a field is used as measure.
                if c_name in measure_names or role == "measure":
                    norm = raw.lower()
                    if norm in {"text", "string", "varchar", "nvarchar"}:
                        raw = "double"
                cols.append(
                    PBIXColumn(
                        name=c_name,
                        data_type=raw,
                        is_hidden=bool(c.get("is_hidden") or c.get("isHidden") or False),
                    )
                )
            # Carry source_connection from the abstract spec entity so that
            # TMSLBuilder can generate a real Power Query M expression.
            src_conn: dict[str, Any] | None = None
            raw_conn = entity.get("source_connection")
            if isinstance(raw_conn, dict) and raw_conn:
                src_conn = raw_conn
            tables.append(PBIXTable(name=t_name, columns=cols, measures=measures_by_table.get(t_name, []), source_connection=src_conn))

        table_names = {t.name for t in tables}
        relationships: list[PBIXRelationship] = []
        for r in rels:
            if not isinstance(r, dict):
                continue
            ft = str(r.get("from_table") or r.get("fromTable") or "")
            fc = str(r.get("from_column") or r.get("fromColumn") or "")
            tt = str(r.get("to_table") or r.get("toTable") or "")
            tc = str(r.get("to_column") or r.get("toColumn") or "")
            if ft and tt and fc and tc and ft in table_names and tt in table_names:
                relationships.append(PBIXRelationship(from_table=ft, from_column=fc, to_table=tt, to_column=tc))

        if not relationships:
            for healed in self._heal_relationships_from_lineage(spec, table_names):
                relationships.append(healed)

        relationships = self._dedupe_relationships(relationships)

        model_name = str(spec.get("id") or spec.get("name") or "Model")
        return PBIXSemanticModel(name=model_name, tables=tables, relationships=relationships)

    def _dedupe_relationships(self, relationships: list[PBIXRelationship]) -> list[PBIXRelationship]:
        """Keep at most one active relationship per source column.

        Power BI cannot model multiple active relationships from the same
        source column path without ambiguity. When overlapping candidates are
        present, keep the first valid relation and drop later conflicts.
        """
        seen_exact: set[tuple[str, str, str, str]] = set()
        seen_source: dict[tuple[str, str], PBIXRelationship] = {}
        deduped: list[PBIXRelationship] = []

        for rel in relationships:
            exact_key = (rel.from_table, rel.from_column, rel.to_table, rel.to_column)
            source_key = (rel.from_table, rel.from_column)

            if exact_key in seen_exact:
                continue

            existing = seen_source.get(source_key)
            if existing is not None and (
                existing.to_table != rel.to_table or existing.to_column != rel.to_column
            ):
                _log.warning(
                    "Dropping ambiguous relationship %s.%s -> %s.%s (already kept %s.%s -> %s.%s)",
                    rel.from_table,
                    rel.from_column,
                    rel.to_table,
                    rel.to_column,
                    existing.from_table,
                    existing.from_column,
                    existing.to_table,
                    existing.to_column,
                )
                continue

            seen_exact.add(exact_key)
            seen_source[source_key] = rel
            deduped.append(rel)

        return deduped

    def _heal_relationships_from_lineage(
        self,
        spec: dict[str, Any],
        table_names: set[str],
    ) -> list[PBIXRelationship]:
        lineage = spec.get("data_lineage", {}) if isinstance(spec.get("data_lineage"), dict) else {}
        joins = lineage.get("joins", []) if isinstance(lineage.get("joins"), list) else []
        healed: list[PBIXRelationship] = []
        for j in joins:
            if not isinstance(j, dict):
                continue
            left_raw = str(j.get("left_table") or "")
            right_raw = str(j.get("right_table") or "")
            left = left_raw.strip("[]").split("].[")[-1].replace("]", "")
            right = right_raw.strip("[]").split("].[")[-1].replace("]", "")
            if left not in table_names or right not in table_names:
                continue
            candidate = self._pick_join_columns(left, right, spec)
            if not candidate:
                continue
            from_col, to_col = candidate
            healed.append(PBIXRelationship(from_table=left, from_column=from_col, to_table=right, to_column=to_col))
        return healed

    def _pick_join_columns(self, left: str, right: str, spec: dict[str, Any]) -> tuple[str, str] | None:
        semantic = spec.get("semantic_model", {}) if isinstance(spec.get("semantic_model"), dict) else {}
        entities = semantic.get("entities", []) if isinstance(semantic.get("entities"), list) else []
        cols_map: dict[str, set[str]] = {}
        for e in entities:
            if isinstance(e, dict):
                n = str(e.get("name") or e.get("id") or "")
                cols = e.get("columns", []) if isinstance(e.get("columns"), list) else []
                cols_map[n] = {str(c.get("name")) for c in cols if isinstance(c, dict) and c.get("name")}
        left_cols = cols_map.get(left, set())
        right_cols = cols_map.get(right, set())
        common = sorted(left_cols.intersection(right_cols))
        if common:
            return (common[0], common[0])
        for lc in left_cols:
            if lc.endswith("Key"):
                base = lc[:-3]
                for rc in right_cols:
                    if rc == lc or rc.startswith(base):
                        return (lc, rc)
        return None

    def _layout_from_abstract_spec(self, spec: dict[str, Any], model: PBIXSemanticModel) -> dict[str, Any]:
        """Build the full Report/Layout JSON using PBIXReportBuilder.

        Falls back to the schema-only PBIXLayoutBuilder when the dashboard_spec
        has no pages (e.g. pure semantic-model export with no visual definitions).
        """
        try:
            from engine.creation.pbix_report_builder import PBIXReportBuilder
            dashboard = spec.get("dashboard_spec") if isinstance(spec.get("dashboard_spec"), dict) else {}
            pages = dashboard.get("pages") if isinstance(dashboard.get("pages"), list) else []
            if pages:
                layout = PBIXReportBuilder().build(
                    spec, model,
                    report_config_version=_REPORT_CONFIG_VERSION,
                    theme_name=_THEME_NAME,
                )
                _log.info(
                    "pbix_layout: pages=%d sections=%d",
                    len(pages),
                    len(layout.get("sections", [])),
                )
                return layout
        except Exception as exc:
            _log.warning("PBIXReportBuilder failed (%s) — falling back to schema-only layout", exc)

        # Fallback: schema-only layout (one page per table)
        return PBIXLayoutBuilder().build(model)

    def _categorical_container(
        self,
        visual_id: str,
        table: str,
        dim_col: str,
        measure_col: str,
        visual_type: str,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> dict[str, Any]:
        dim_ref = f"{table}.{dim_col}"
        meas_ref = f"{table}.{measure_col}"
        query = {
            "Version": 2,
            "From": [{"Name": "t", "Entity": table, "Type": 0}],
            "Select": [
                {"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": dim_col}, "Name": dim_ref},
                {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": measure_col}}, "Function": 0}, "Name": meas_ref},
            ],
            "OrderBy": [],
        }
        category_role = "Category"
        value_role = "Y" if visual_type == "columnChart" else "Values"
        config = {
            "name": f"{visual_type}_{visual_id}",
            "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0}}],
            "singleVisual": {
                "visualType": visual_type,
                "projections": {value_role: [{"queryRef": meas_ref, "active": True}], category_role: [{"queryRef": dim_ref}]},
                "prototypeQuery": query,
            },
        }
        return {
            "x": x,
            "y": y,
            "z": 0,
            "width": w,
            "height": h,
            "config": json.dumps(config, separators=(",", ":")),
            "filters": "[]",
            "query": json.dumps(query, separators=(",", ":")),
            "dataTransforms": "{}",
        }

    def _text_box_container(self, visual_id: str, text: str, x: int, y: int, w: int, h: int) -> dict[str, Any]:
        config = {
            "name": f"textbox_{visual_id}",
            "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0}}],
            "singleVisual": {
                "visualType": "textbox",
                "objects": {
                    "general": [{"properties": {"paragraphs": {"expr": {"Literal": {"Value": json.dumps(text)}}}}}]
                },
            },
        }
        return {
            "x": x,
            "y": y,
            "z": 0,
            "width": w,
            "height": h,
            "config": json.dumps(config, separators=(",", ":")),
            "filters": "[]",
            "query": "{}",
            "dataTransforms": "{}",
        }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _stable_id(text: str) -> str:
    """Deterministic short GUID-like string for lineageTags."""
    h = hashlib.md5(text.encode()).hexdigest()  # noqa: S324 — not security-sensitive
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ---------------------------------------------------------------------------
# .pbit (Power BI Template) generator
#
# .pbit uses DataModelSchema (TMSL JSON) instead of the binary DataModel part.
# This is the correct format for schema-only/validation use cases — it opens
# in all versions of Power BI Desktop without needing a Vertipaq binary.
# ---------------------------------------------------------------------------

def _pbit_content_types_xml() -> bytes:
    # Matches the compact UTF-8 format Power BI Desktop produces.
    # Empty ContentType="" for binary/unknown parts; application/json only for
    # Settings and Metadata which are explicitly JSON by Power BI conventions.
    # NO Default Extension="rels" — real Power BI files do not include it.
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="json" ContentType="" />'
        '<Override PartName="/Version" ContentType="" />'
        '<Override PartName="/DiagramLayout" ContentType="" />'
        '<Override PartName="/Report/Layout" ContentType="" />'
        '<Override PartName="/Settings" ContentType="application/json" />'
        '<Override PartName="/Metadata" ContentType="application/json" />'
        '<Override PartName="/SecurityBindings" ContentType="" />'
        '<Override PartName="/DataModelSchema" ContentType="" />'
        '</Types>'
    )
    return xml.encode("utf-8")



class PBITPackager:
    """Assembles a Power BI Template (.pbit) OPC ZIP archive.

    A .pbit file stores the semantic model as TMSL JSON (DataModelSchema part)
    instead of a binary Vertipaq database.  Power BI Desktop opens it, displays
    the full data model (tables, columns, measures, relationships) and prompts
    the user to connect to data sources.

    This is the correct output format when no live data is available, making it
    ideal for schema validation after Phase 0 + Phase 1 extraction.
    """

    def pack(
        self,
        model: PBIXSemanticModel,
        output: Path | None = None,
        *,
        layout_override: dict[str, Any] | None = None,
    ) -> bytes:
        layout_builder = PBIXLayoutBuilder()
        diagram_builder = PBIXDiagramLayoutBuilder()

        tmsl = TMSLBuilder().build(model)
        # Compact JSON + UTF-16LE (no BOM) — matches Power BI Desktop's own output
        schema_json = json.dumps(tmsl, separators=(",", ":"), ensure_ascii=False)

        layout_payload = layout_override if layout_override is not None else layout_builder.build(model)
        layout_json = json.dumps(layout_payload, separators=(",", ":"), ensure_ascii=False)
        diagram_json = json.dumps(diagram_builder.build(model), separators=(",", ":"), ensure_ascii=False)
        metadata_json = json.dumps(_build_metadata(model), separators=(",", ":"), ensure_ascii=False)
        settings_json = json.dumps(_build_settings(), separators=(",", ":"), ensure_ascii=False)

        buf = io.BytesIO()
        # NO _rels/.rels and NO Report/_rels/Layout.rels — real Power BI files
        # do not contain relationship part files in the outer package.
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("[Content_Types].xml", _pbit_content_types_xml())
            zf.writestr("DataModelSchema", _pbix_text_bytes(schema_json))
            zf.writestr("Report/Layout", _pbix_text_bytes(layout_json))
            zf.writestr("DiagramLayout", _pbix_text_bytes(diagram_json))
            zf.writestr("Metadata", _pbix_text_bytes(metadata_json))
            zf.writestr("SecurityBindings", b"")
            zf.writestr("Settings", _pbix_text_bytes(settings_json))
            zf.writestr("Version", _pbix_text_bytes(_PBIX_FORMAT_VERSION))

        payload = buf.getvalue()
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
            _log.info("pbit written: path=%s size=%d bytes", output, len(payload))
        return payload


def verify_pbit(path: Path) -> PBIXVerificationResult:
    """Structural integrity check for a generated .pbit file."""
    _PBIT_REQUIRED = frozenset({
        "[Content_Types].xml",
        "DataModelSchema",
        "Report/Layout",
        "DiagramLayout",
        "Metadata",
        "Settings",
        "Version",
    })
    warnings: list[str] = []
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    size = len(raw)

    try:
        outer = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        return PBIXVerificationResult(
            path=str(path), size_bytes=size, sha256=sha256,
            parts_found=[], parts_missing=list(_PBIT_REQUIRED),
            inner_model_tables=[], is_structurally_valid=False,
            warnings=[f"ZIP is corrupt: {exc}"],
        )

    names = set(outer.namelist())
    parts_found = sorted(n for n in names if n in _PBIT_REQUIRED or n == "_rels/.rels")
    parts_missing = sorted(_PBIT_REQUIRED - names)

    inner_tables: list[str] = []
    if "DataModelSchema" in names:
        try:
            raw_schema = outer.read("DataModelSchema")
            for enc in ("utf-16le", "utf-8"):
                try:
                    tmsl = json.loads(raw_schema.decode(enc))
                    inner_tables = [t["name"] for t in tmsl.get("model", {}).get("tables", [])]
                    break
                except Exception:
                    continue
        except Exception as exc:
            warnings.append(f"DataModelSchema parse failed: {exc}")
    else:
        warnings.append("DataModelSchema part absent")

    return PBIXVerificationResult(
        path=str(path), size_bytes=size, sha256=sha256,
        parts_found=parts_found, parts_missing=parts_missing,
        inner_model_tables=inner_tables,
        is_structurally_valid=len(parts_missing) == 0,
        warnings=warnings,
    )


class PBITGenerator:
    """High-level API: AbstractSpec / semantic model dict -> .pbit template file.

    .pbit files open in Power BI Desktop, show the complete data model
    (tables, columns, measures, relationships) and prompt the user to
    connect data sources.  No binary Vertipaq database required.

    Usage::

        gen = PBITGenerator()
        result = gen.generate_from_abstract_spec(abstract_spec, Path("output/model.pbit"))
    """

    def __init__(self) -> None:
        self._packager = PBITPackager()
        # Reuse the AbstractSpec -> PBIXSemanticModel logic from PBIXGenerator
        self._pbix_gen = PBIXGenerator()

    def generate(
        self,
        model: PBIXSemanticModel | dict[str, Any],
        output_path: Path,
        *,
        verify: bool = True,
    ) -> dict[str, Any]:
        if isinstance(model, dict):
            model = PBIXSemanticModel.from_dict(model)
        payload = self._packager.pack(model, output=output_path, layout_override=self._minimal_layout(model))
        result: dict[str, Any] = {
            "ok": True,
            "path": str(output_path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if verify:
            vr = verify_pbit(output_path)
            result["verification"] = vr.as_dict()
            result["ok"] = vr.is_structurally_valid
        return result

    def generate_from_abstract_spec(
        self,
        abstract_spec: dict[str, Any],
        output_path: Path,
        *,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Generate a .pbit from an AbstractSpec dict (same input as PBIXGenerator)."""
        model = self._pbix_gen._model_from_abstract_spec(abstract_spec)
        payload = self._packager.pack(model, output=output_path, layout_override=self._minimal_layout(model))

        result: dict[str, Any] = {
            "ok": True,
            "path": str(output_path),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "audit": {
                "tables_count": len(model.tables),
                "relationships_count": len(model.relationships),
                "measures_count": sum(len(t.measures) for t in model.tables),
            },
        }
        if verify:
            vr = verify_pbit(output_path)
            result["verification"] = vr.as_dict()
            result["ok"] = vr.is_structurally_valid
        return result

    def _minimal_layout(self, model: PBIXSemanticModel) -> dict[str, Any]:
        """Build a schema-only report layout with no visuals.

        The .pbit template should preserve the tabular model but avoid
        importing dashboard visuals from the source workbook. This keeps the
        generated template minimal while remaining structurally valid.
        """
        page_name = model.name or "Model"
        return {
            "id": 0,
            "resourcePackages": [],
            "sections": [
                {
                    "id": 0,
                    "name": "Section1",
                    "displayName": page_name,
                    "filters": "[]",
                    "ordinal": 0,
                    "visualContainers": [],
                    "config": json.dumps(
                        {"defaultFilters": [], "visibility": 0},
                        separators=(",", ":"),
                    ),
                    "width": 1280,
                    "height": 720,
                    "displayOption": 0,
                }
            ],
            "config": json.dumps(
                {
                    "version": _REPORT_CONFIG_VERSION,
                    "themeCollection": {
                        "baseTheme": {
                            "name": _THEME_NAME,
                            "version": _REPORT_CONFIG_VERSION,
                            "type": 2,
                        }
                    },
                },
                separators=(",", ":"),
            ),
            "filters": "[]",
        }
