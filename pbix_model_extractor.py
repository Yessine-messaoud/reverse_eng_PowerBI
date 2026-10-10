"""
pbix_model_extractor.py — Extract semantic model from an existing .pbix file.

A .pbix file is an OPC ZIP archive whose DataModel part is itself an inner ZIP
containing the TMSL database definition (model.json / database.json).

This module reads that TMSL JSON, converts it back into the AbstractSpec
semantic_model format used by the rest of the pipeline, and returns both:
  - A typed PBIXSemanticModel  (for PBIXReportBuilder visual binding)
  - An AbstractSpec-format dict (for SemanticBinder field resolution)
  - The raw DataModel bytes     (preserved verbatim for the merge step)

None of the above rebuilds or regenerates the semantic model.

Key classes
-----------
ExtractedPBIX    Container for all extracted parts
PBIXModelExtractor   Main extractor — opens .pbix and parses TMSL
PBIXMerger       Clone a base .pbix and inject a new Report/Layout
"""

from __future__ import annotations

import io
import json
import logging
import copy
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TMSL → PBIXSemanticModel type mapping
# ---------------------------------------------------------------------------
# TMSL "dataType" values produced by TMSLBuilder._column():
#   string, int64, double, decimal, boolean, dateTime, binary
# We map them to SQL/generic type strings accepted by PBIXColumn.data_type,
# which are then converted back through _tmsl_type() in pbix_generator.py.

_TMSL_DATATYPE_PASSTHROUGH: frozenset[str] = frozenset({
    "string", "decimal", "double", "boolean", "binary",
})

_TMSL_DATATYPE_REMAP: dict[str, str] = {
    "int64": "integer",    # _tmsl_type("integer") -> "int64"
    "dateTime": "datetime", # _tmsl_type("datetime") -> "dateTime"
}


def _col_data_type(tmsl_dtype: str) -> str:
    """Convert a TMSL dataType string to a PBIXColumn.data_type value."""
    dt = str(tmsl_dtype or "string").strip()
    return _TMSL_DATATYPE_REMAP.get(dt, dt if dt in _TMSL_DATATYPE_PASSTHROUGH else "string")


def _col_pbi_type(tmsl_dtype: str) -> str:
    """Convert TMSL dataType → AbstractSpec pbi_type string."""
    mapping = {
        "string": "text",
        "int64": "integer",
        "double": "decimal",
        "decimal": "decimal",
        "boolean": "boolean",
        "dateTime": "dateTime",
        "binary": "binary",
    }
    return mapping.get(str(tmsl_dtype or "string").strip(), "text")


def _col_role(tmsl_dtype: str, summarize_by: str) -> str:
    """Infer column role from TMSL metadata.

    Columns whose dataType is numeric AND summarizeBy is not "none" are
    treated as measure-like raw columns.  All others are dimensions.
    This heuristic matches how TMSLBuilder.build() writes summarizeBy.
    """
    numeric = tmsl_dtype in ("int64", "double", "decimal")
    aggregable = str(summarize_by or "none").lower() not in ("none", "")
    return "measure" if (numeric and aggregable) else "dimension"


def _cardinality_from_tmsl(from_card: str, to_card: str) -> str:
    """Reconstruct AbstractSpec cardinality from TMSL from/toCardinality values."""
    mapping = {
        ("many", "one"):  "manyToOne",
        ("one",  "one"):  "oneToOne",
        ("many", "many"): "manyToMany",
        ("one",  "many"): "oneToMany",
    }
    key = (str(from_card or "many").lower(), str(to_card or "one").lower())
    return mapping.get(key, "manyToOne")


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class ExtractedPBIX:
    """All data extracted from a .pbix file."""

    path: Path
    datamodel_bytes: bytes          # Raw DataModel inner ZIP — preserved verbatim
    tmsl: dict[str, Any]            # Parsed model.json / database.json
    pbix_model: Any                 # PBIXSemanticModel instance (typed)
    semantic_model: dict[str, Any]  # AbstractSpec-compatible format
    existing_layout: dict[str, Any] | None   # Report/Layout JSON (may be None)
    metadata: dict[str, Any]        # Metadata part JSON
    compatibility_level: int
    table_names: list[str] = field(default_factory=list)
    measure_names: list[str] = field(default_factory=list)
    relationship_count: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "compatibility_level": self.compatibility_level,
            "tables": self.table_names,
            "measures": self.measure_names,
            "relationships": self.relationship_count,
            "has_existing_layout": self.existing_layout is not None,
        }


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class PBIXModelExtractor:
    """Extract semantic model and raw DataModel bytes from an existing .pbix.

    Usage::

        ex = PBIXModelExtractor().extract(Path("input/base_datamodel.pbix"))
        print(ex.table_names)          # ["Sales", "Customers", ...]
        # Pass ex.pbix_model to PBIXReportBuilder
        # Pass ex.datamodel_bytes to PBIXMerger (preserved verbatim)
    """

    def extract(self, pbix_path: Path) -> ExtractedPBIX:
        if not pbix_path.exists():
            raise FileNotFoundError(f"PBIX not found: {pbix_path}")

        _log.info("pbix_extract: path=%s size=%d bytes", pbix_path, pbix_path.stat().st_size)

        try:
            outer = zipfile.ZipFile(pbix_path, "r")
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Not a valid PBIX/ZIP file: {pbix_path}") from exc

        parts = set(outer.namelist())

        # ── DataModel ──────────────────────────────────────────────────────
        if "DataModel" not in parts:
            raise ValueError("PBIX has no DataModel part — cannot extract semantic model")

        datamodel_bytes = outer.read("DataModel")
        tmsl, compat = self._parse_tmsl(datamodel_bytes)

        # ── Metadata ───────────────────────────────────────────────────────
        metadata: dict[str, Any] = {}
        if "Metadata" in parts:
            try:
                raw = outer.read("Metadata")
                metadata = self._decode_json_part(raw)
            except Exception as exc:
                _log.warning("Metadata parse failed: %s", exc)

        # ── Existing Report/Layout ─────────────────────────────────────────
        existing_layout: dict[str, Any] | None = None
        if "Report/Layout" in parts:
            try:
                raw = outer.read("Report/Layout")
                existing_layout = self._decode_json_part(raw)
                pages_count = len(existing_layout.get("sections") or [])
                _log.info("pbix_extract: existing layout has %d page(s)", pages_count)
            except Exception as exc:
                _log.warning("Report/Layout parse failed: %s", exc)

        outer.close()

        # ── Build typed models ─────────────────────────────────────────────
        pbix_model = self._tmsl_to_pbix_model(tmsl)
        semantic_model = self._tmsl_to_abstract_semantic(tmsl)

        table_names = [t.name for t in pbix_model.tables]
        measure_names = [
            m.name
            for t in pbix_model.tables
            for m in t.measures
        ]
        relationship_count = len(pbix_model.relationships)

        _log.info(
            "pbix_extract: tables=%d measures=%d relationships=%d compat=%d",
            len(table_names), len(measure_names), relationship_count, compat,
        )

        return ExtractedPBIX(
            path=pbix_path,
            datamodel_bytes=datamodel_bytes,
            tmsl=tmsl,
            pbix_model=pbix_model,
            semantic_model=semantic_model,
            existing_layout=existing_layout,
            metadata=metadata,
            compatibility_level=compat,
            table_names=table_names,
            measure_names=measure_names,
            relationship_count=relationship_count,
        )

    # ── Private helpers ────────────────────────────────────────────────────

    @staticmethod
    def _decode_json_part(raw: bytes) -> dict[str, Any]:
        for enc in ("utf-16le", "utf-8"):
            try:
                return json.loads(raw.decode(enc))
            except Exception:
                continue
        return json.loads(raw)

    @staticmethod
    def _parse_tmsl(datamodel_bytes: bytes) -> tuple[dict[str, Any], int]:
        """Open the DataModel inner ZIP and parse model.json.

        Returns ({}, 1601) when the DataModel is in ABF/Vertipaq binary format
        (Power BI V3 enhanced dataset format) instead of the legacy inner-ZIP
        TMSL format.  The raw bytes are still preserved verbatim for PBIXMerger.
        """
        try:
            inner = zipfile.ZipFile(io.BytesIO(datamodel_bytes))
        except zipfile.BadZipFile:
            # ABF / XPress9-compressed binary — not parseable as TMSL
            _log.warning(
                "_parse_tmsl: DataModel is in ABF/V3 binary format (not an inner ZIP). "
                "TMSL extraction skipped; semantic model must come from the abstract spec."
            )
            return {}, 1601

        candidates = [n for n in inner.namelist() if n in ("model.json", "database.json")]
        if not candidates:
            raise ValueError(f"DataModel inner ZIP has no model.json/database.json (found: {inner.namelist()})")

        raw = inner.read(candidates[0])
        try:
            tmsl = json.loads(raw.decode("utf-8"))
        except Exception:
            tmsl = json.loads(raw.decode("utf-16le"))

        compat = int(tmsl.get("compatibilityLevel") or 1550)
        return tmsl, compat

    def _tmsl_to_pbix_model(self, tmsl: dict[str, Any]) -> Any:
        """Convert TMSL JSON → PBIXSemanticModel typed instance.

        Constructs PBIXTable / PBIXColumn / PBIXMeasure / PBIXRelationship
        objects directly from TMSL — bypassing from_dict() to avoid any
        type-remapping ambiguity with TMSL-native dataType strings.
        """
        from engine.creation.pbix_generator import (
            PBIXSemanticModel, PBIXTable, PBIXColumn, PBIXMeasure,
            PBIXRelationship, PBICardinality, PBICrossFilter,
        )

        model_node = tmsl.get("model", {}) if isinstance(tmsl.get("model"), dict) else {}

        tables: list[PBIXTable] = []
        for tbl in model_node.get("tables", []) if isinstance(model_node.get("tables"), list) else []:
            if not isinstance(tbl, dict):
                continue
            t_name = str(tbl.get("name") or "Table").strip()

            cols: list[PBIXColumn] = []
            for col in tbl.get("columns", []) if isinstance(tbl.get("columns"), list) else []:
                if not isinstance(col, dict):
                    continue
                c_name = str(col.get("name") or "Column").strip()
                c_dtype = str(col.get("dataType") or "string")
                c_hidden = bool(col.get("isHidden") or False)
                c_fmt = str(col.get("formatString") or "")
                cols.append(PBIXColumn(
                    name=c_name,
                    data_type=_col_data_type(c_dtype),
                    source_column=str(col.get("sourceColumn") or c_name),
                    format_string=c_fmt,
                    is_hidden=c_hidden,
                    description=str(col.get("description") or ""),
                ))

            measures: list[PBIXMeasure] = []
            for m in tbl.get("measures", []) if isinstance(tbl.get("measures"), list) else []:
                if not isinstance(m, dict):
                    continue
                m_name = str(m.get("name") or "Measure").strip()
                m_expr = str(m.get("expression") or "0").strip()
                measures.append(PBIXMeasure(
                    name=m_name,
                    expression=m_expr,
                    format_string=str(m.get("formatString") or ""),
                    description=str(m.get("description") or ""),
                ))

            tables.append(PBIXTable(
                name=t_name,
                columns=cols,
                measures=measures,
                description=str(tbl.get("description") or ""),
                is_date_table=bool(tbl.get("isDateTable") or False),
            ))

        relationships: list[PBIXRelationship] = []
        for rel in model_node.get("relationships", []) if isinstance(model_node.get("relationships"), list) else []:
            if not isinstance(rel, dict):
                continue
            ft = str(rel.get("fromTable") or "").strip()
            fc = str(rel.get("fromColumn") or "").strip()
            tt = str(rel.get("toTable") or "").strip()
            tc = str(rel.get("toColumn") or "").strip()
            if not (ft and fc and tt and tc):
                continue
            card_str = _cardinality_from_tmsl(
                rel.get("fromCardinality", "many"),
                rel.get("toCardinality", "one"),
            )
            try:
                cardinality = PBICardinality(card_str)
            except ValueError:
                cardinality = PBICardinality.MANY_TO_ONE
            cross_raw = str(rel.get("crossFilteringBehavior") or "OneDirection").lower()
            cross = PBICrossFilter.BOTH if "both" in cross_raw else PBICrossFilter.SINGLE
            relationships.append(PBIXRelationship(
                from_table=ft, from_column=fc,
                to_table=tt, to_column=tc,
                cardinality=cardinality,
                cross_filter=cross,
                is_active=bool(rel.get("isActive", True)),
            ))

        name = str(model_node.get("name") or "Model")
        culture = str(model_node.get("culture") or "en-US")
        return PBIXSemanticModel(
            name=name, tables=tables, relationships=relationships, culture=culture,
        )

    def _tmsl_to_abstract_semantic(self, tmsl: dict[str, Any]) -> dict[str, Any]:
        """Convert TMSL JSON → AbstractSpec semantic_model dict.

        Output matches the shape consumed by SemanticBinder and
        PBIXGenerator._model_from_abstract_spec().
        """
        model_node = tmsl.get("model", {}) if isinstance(tmsl.get("model"), dict) else {}

        entities: list[dict] = []
        all_measures: list[dict] = []
        fact_table = ""

        for tbl in model_node.get("tables", []) if isinstance(model_node.get("tables"), list) else []:
            if not isinstance(tbl, dict):
                continue
            t_name = str(tbl.get("name") or "Table").strip()

            columns: list[dict] = []
            has_measures = False
            for col in tbl.get("columns", []) if isinstance(tbl.get("columns"), list) else []:
                if not isinstance(col, dict):
                    continue
                c_name = str(col.get("name") or "").strip()
                if not c_name:
                    continue
                c_dtype = str(col.get("dataType") or "string")
                c_summarize = str(col.get("summarizeBy") or "none")
                columns.append({
                    "name": c_name,
                    "pbi_type": _col_pbi_type(c_dtype),
                    "role": _col_role(c_dtype, c_summarize),
                    "label": c_name,
                    "is_hidden": bool(col.get("isHidden") or False),
                })

            for m in tbl.get("measures", []) if isinstance(tbl.get("measures"), list) else []:
                if not isinstance(m, dict):
                    continue
                m_name = str(m.get("name") or "").strip()
                m_expr = str(m.get("expression") or "0").strip()
                if not m_name:
                    continue
                all_measures.append({
                    "name": m_name,
                    "expression": m_expr,
                    "format_string": str(m.get("formatString") or ""),
                    "description": str(m.get("description") or ""),
                    "source_columns": [{"table": t_name, "column": ""}],
                })
                has_measures = True

            entities.append({"name": t_name, "id": t_name, "columns": columns})
            if has_measures and not fact_table:
                fact_table = t_name

        relationships: list[dict] = []
        for rel in model_node.get("relationships", []) if isinstance(model_node.get("relationships"), list) else []:
            if not isinstance(rel, dict):
                continue
            ft = str(rel.get("fromTable") or "").strip()
            fc = str(rel.get("fromColumn") or "").strip()
            tt = str(rel.get("toTable") or "").strip()
            tc = str(rel.get("toColumn") or "").strip()
            if ft and fc and tt and tc:
                relationships.append({
                    "from_table": ft, "from_column": fc,
                    "to_table": tt, "to_column": tc,
                    "cardinality": _cardinality_from_tmsl(
                        rel.get("fromCardinality", "many"),
                        rel.get("toCardinality", "one"),
                    ),
                })

        return {
            "entities": entities,
            "measures": all_measures,
            "relationships": relationships,
            "fact_table": fact_table,
            "dimensions": [],
            "grain": "",
        }


# ---------------------------------------------------------------------------
# PBIX Merger — clone base PBIX, replace only Report/Layout
# ---------------------------------------------------------------------------

class PBIXMerger:
    """Clone an existing .pbix and inject a new Report/Layout.

    The DataModel, Metadata, Settings, Version, SecurityBindings and any other
    OPC parts from the base file are preserved byte-for-byte.  Only
    Report/Layout (and optionally DiagramLayout) is replaced.

    This is the correct strategy when we already have a validated semantic
    model in the base PBIX and only need to inject a new dashboard definition.

    Usage::

        merger = PBIXMerger()
        merger.merge(
            base_pbix=Path("input/base_datamodel.pbix"),
            new_layout=layout_dict,
            output=Path("output/demo_dashboard.pbix"),
        )
    """

    @staticmethod
    def _encode(text: str) -> bytes:
        """PBIX textual parts are UTF-16LE (no BOM)."""
        return text.encode("utf-16le")

    @staticmethod
    def _clone_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
        """Deep-copy ZipInfo so we preserve container metadata on rewrite."""
        return copy.copy(info)

    def merge(
        self,
        base_pbix: Path,
        new_layout: dict[str, Any],
        output: Path,
        *,
        new_diagram_layout: dict[str, Any] | None = None,
    ) -> bytes:
        """Write *output* by cloning *base_pbix* with *new_layout* injected.

        Parameters
        ----------
        base_pbix:
            Source .pbix whose DataModel (and all other parts) are preserved.
        new_layout:
            Report/Layout JSON dict produced by PBIXReportBuilder.
        output:
            Destination .pbix path.  Parent directories are created if absent.
        new_diagram_layout:
            Optional replacement for DiagramLayout.  When None the original
            DiagramLayout from base_pbix is kept.

        Returns
        -------
        bytes
            Raw .pbix archive content.
        """
        if not base_pbix.exists():
            raise FileNotFoundError(f"Base PBIX not found: {base_pbix}")

        layout_bytes = self._encode(json.dumps(new_layout, indent=2, ensure_ascii=False))

        buf = io.BytesIO()
        with zipfile.ZipFile(base_pbix, "r") as src:
            names = src.namelist()
            with zipfile.ZipFile(buf, "w") as dst:
                for name in names:
                    if name == "Report/Layout":
                        # Replace with the new dashboard layout
                        base_info = self._clone_info(src.getinfo(name))
                        dst.writestr(base_info, layout_bytes)
                        _log.debug("pbix_merge: replaced %s", name)
                    elif name == "DiagramLayout" and new_diagram_layout is not None:
                        dl_bytes = self._encode(json.dumps(new_diagram_layout, indent=2, ensure_ascii=False))
                        base_info = self._clone_info(src.getinfo(name))
                        dst.writestr(base_info, dl_bytes)
                        _log.debug("pbix_merge: replaced DiagramLayout")
                    else:
                        # Preserve verbatim (DataModel, Metadata, Settings, Version, …)
                        info = src.getinfo(name)
                        dst.writestr(self._clone_info(info), src.read(name))

                # If base PBIX had no Report/Layout part at all, add it now
                if "Report/Layout" not in names:
                    li = zipfile.ZipInfo("Report/Layout")
                    li.compress_type = zipfile.ZIP_DEFLATED
                    dst.writestr(li, layout_bytes)
                    _log.info("pbix_merge: Report/Layout was absent in base — added")

        payload = buf.getvalue()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        _log.info(
            "pbix_merge: written path=%s size=%d bytes (base=%s)",
            output, len(payload), base_pbix.name,
        )
        return payload
