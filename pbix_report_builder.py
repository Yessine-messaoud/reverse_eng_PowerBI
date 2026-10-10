"""
pbix_report_builder.py
======================
Full Power BI Report/Layout builder.

Converts an AbstractSpec (dashboard_spec + semantic_model + data_lineage) into
a complete Power BI Report/Layout JSON that Power BI Desktop renders correctly.

Key capabilities
----------------
* One Power BI page per Tableau dashboard (zero page loss)
* Full visual-type mapping table (11 chart types + slicer fallback)
* Correct field bindings: dimensions, measures, legends, categories
* Layout reconstruction from Tableau position metadata
* Slicer generation from dashboard-level filters
* Color/theme injection from source Tableau palette

Visual type mapping
-------------------
Tableau mark type  →  Power BI visualType
bar / Bar          →  clusteredBarChart
line / Line        →  lineChart
area               →  areaChart
circle / scatter   →  scatterChart
pie / Pie          →  pieChart
text / table       →  tableEx
treemap            →  treemap
map / Map          →  map
kpi                →  card
gantt              →  clusteredBarChart (fallback)
image              →  image (textbox placeholder)
<unknown>          →  tableEx (safe fallback)
"""

from __future__ import annotations

import json
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from engine.creation.pbix_generator import PBIXSemanticModel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PAGE_W = 1280
_PAGE_H = 720

# Tableau default canvas; used for proportional position scaling
_TABLEAU_W = 1000
_TABLEAU_H = 800

# ---------------------------------------------------------------------------
# Visual type mapping
# ---------------------------------------------------------------------------

# Tableau logical type → Power BI Desktop visualType string
_PBI_VISUAL_TYPE: dict[str, str] = {
    "bar":          "clusteredBarChart",
    "column":       "clusteredColumnChart",
    "line":         "lineChart",
    "area":         "areaChart",
    "scatter":      "scatterChart",
    "circle":       "scatterChart",
    "pie":          "pieChart",
    "donut":        "donutChart",
    "table":        "tableEx",
    "tablix":       "tableEx",
    "crosstab":     "matrix",
    "matrix":       "matrix",
    "kpi":          "card",
    "card":         "card",
    "map":          "map",
    "treemap":      "treemap",
    "gantt":        "clusteredBarChart",
    "waterfall":    "waterfallChart",
    "funnel":       "funnel",
    "combo":        "lineClusteredColumnComboChart",
    "image":        "image",
    "text":         "textbox",
}

# Field role names per visual type
_ROLES: dict[str, dict[str, str]] = {
    "clusteredBarChart":             {"x": "Category", "y": "Y",        "legend": "Series"},
    "clusteredColumnChart":          {"x": "Category", "y": "Y",        "legend": "Series"},
    "lineChart":                     {"x": "Category", "y": "Y",        "legend": "Series"},
    "areaChart":                     {"x": "Category", "y": "Y",        "legend": "Series"},
    "scatterChart":                  {"x": "X",        "y": "Y",        "legend": "Details", "size": "Size"},
    "pieChart":                      {"x": "Category", "y": "Values",   "legend": "Category"},
    "donutChart":                    {"x": "Category", "y": "Values",   "legend": "Category"},
    "tableEx":                       {"values": "Values"},
    "matrix":                        {"x": "Rows",     "y": "Values",   "legend": "Columns"},
    "card":                          {"y": "Values"},
    "map":                           {"x": "Location", "y": "Size",     "legend": "Color"},
    "treemap":                       {"x": "Category", "y": "Values",   "legend": "Details"},
    "waterfallChart":                {"x": "Category", "y": "Y"},
    "funnel":                        {"x": "Category", "y": "Y"},
    "lineClusteredColumnComboChart": {"x": "Category", "y": "ColumnValues", "legend": "LineValues"},
    "image":                         {},
    "textbox":                       {},
}

# DAX aggregation function codes
_AGG_FN: dict[str, int] = {
    "sum": 0, "avg": 1, "average": 1, "min": 2, "max": 3,
    "count": 4, "countrows": 5, "distinctcount": 6, "none": 7,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pbi_type(logical: str) -> str:
    """Normalise a Tableau/logical visual type to a Power BI visualType string."""
    key = str(logical or "").strip().lower()
    return _PBI_VISUAL_TYPE.get(key, "tableEx")


def _roles_for(pbi_type: str) -> dict[str, str]:
    return _ROLES.get(pbi_type, _ROLES["tableEx"])


def _safe_alias(name: str) -> str:
    """Short single-char source alias for M query From clause."""
    return (re.sub(r"[^A-Za-z]", "", str(name or "x"))[:1] or "t").lower()


def _sanitize_name(name: str) -> str:
    """Sanitize visual/slicer/textbox names for CSS selector compatibility.
    
    Replaces spaces and special characters with underscores to prevent
    JavaScript selector errors like: '.resizeHandle.resizeHandleGroup-clusteredBarChart_Feuille 1'
    (spaces are invalid in CSS selectors)
    """
    # Replace spaces with underscores
    sanitized = str(name or "").replace(" ", "_")
    # Replace other problematic characters (but keep alphanumerics, underscores, hyphens)
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", sanitized)
    # Remove consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")
    return sanitized or "visual"


def _agg_code(agg_name: str) -> int:
    return _AGG_FN.get(str(agg_name or "sum").strip().lower(), 0)


def _scale_x(val: Any) -> int:
    try:
        return max(0, min(_PAGE_W - 40, int(float(val) * _PAGE_W / _TABLEAU_W)))
    except (TypeError, ValueError):
        return 20


def _scale_y(val: Any) -> int:
    try:
        return max(0, min(_PAGE_H - 40, int(float(val) * _PAGE_H / _TABLEAU_H)))
    except (TypeError, ValueError):
        return 20


def _scale_w(val: Any, default: int = 400) -> int:
    try:
        return max(100, min(_PAGE_W, int(float(val) * _PAGE_W / _TABLEAU_W)))
    except (TypeError, ValueError):
        return default


def _scale_h(val: Any, default: int = 280) -> int:
    try:
        return max(80, min(_PAGE_H, int(float(val) * _PAGE_H / _TABLEAU_H)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Query / projection builders
# ---------------------------------------------------------------------------

class _QueryBuilder:
    """Builds Power BI prototypeQuery and projection dicts for a single visual.

    Supports multi-table queries: *dim_table* and *meas_table* may differ, in
    which case both are added to the From clause and each field references its
    own source alias.  Power BI resolves cross-table aggregations through the
    model relationships.
    """

    def __init__(
        self,
        dim_table: str,
        meas_table: str,
        dim_col: str | None,
        measure_col: str | None,
        pbi_visual_type: str,
        is_measure: bool = False,
        agg: str = "sum",
        extra_fields: list[str] | None = None,
        legend_col: str | None = None,
        dim_query_ref: str | None = None,
        meas_query_ref: str | None = None,
        legend_query_ref: str | None = None,
    ) -> None:
        self.dim_table = str(dim_table or "Table")
        self.meas_table = str(meas_table or dim_table or "Table")
        self.dim_col = str(dim_col or "") if dim_col else None
        self.measure_col = str(measure_col or "") if measure_col else None
        self.pbi_type = pbi_visual_type
        self.is_measure = is_measure
        self.agg = agg
        self.extra_fields = extra_fields or []
        self.legend_col = str(legend_col or "") if legend_col else None
        self.dim_query_ref = str(dim_query_ref or "").strip() or None
        self.meas_query_ref = str(meas_query_ref or "").strip() or None
        self.legend_query_ref = str(legend_query_ref or "").strip() or None
        self.roles = _roles_for(pbi_visual_type)

        # Assign unique single-char source aliases to each table
        dim_alias = _safe_alias(self.dim_table)
        meas_alias = _safe_alias(self.meas_table)
        # If tables differ but aliases collide, suffix the second one
        if self.dim_table != self.meas_table and dim_alias == meas_alias:
            meas_alias = meas_alias + "m"
        self.dim_alias = dim_alias
        self.meas_alias = meas_alias if self.dim_table != self.meas_table else dim_alias

    # ── Expression helpers ─────────────────────────────────────────────────

    def _source_ref(self, alias: str) -> dict:
        return {"SourceRef": {"Source": alias}}

    def _col_expr(self, col_name: str, alias: str) -> dict:
        return {"Column": {"Expression": self._source_ref(alias), "Property": col_name}}

    def _measure_expr(self, measure_name: str, alias: str) -> dict:
        return {"Measure": {"Expression": self._source_ref(alias), "Property": measure_name}}

    def _agg_expr(self, col_name: str, alias: str) -> dict:
        return {
            "Aggregation": {
                "Expression": self._col_expr(col_name, alias),
                "Function": _agg_code(self.agg),
            }
        }

    def _dim_ref(self, col: str) -> str:
        return self.dim_query_ref or f"{self.dim_table}.{col}"

    def _meas_ref(self, col: str) -> str:
        return self.meas_query_ref or f"{self.meas_table}.{col}"

    def _legend_ref(self, col: str) -> str:
        return self.legend_query_ref or f"{self.dim_table}.{col}"

    # ── Query ──────────────────────────────────────────────────────────────

    def build_query(self) -> dict:
        from_clause: list[dict] = [{"Name": self.dim_alias, "Entity": self.dim_table, "Type": 0}]
        if self.dim_table != self.meas_table:
            from_clause.append({"Name": self.meas_alias, "Entity": self.meas_table, "Type": 0})

        select: list[dict] = []

        if self.dim_col:
            select.append({
                "Column": self._col_expr(self.dim_col, self.dim_alias)["Column"],
                "Name": self._dim_ref(self.dim_col),
            })

        if self.legend_col:
            select.append({
                "Column": self._col_expr(self.legend_col, self.dim_alias)["Column"],
                "Name": self._legend_ref(self.legend_col),
            })

        if self.measure_col:
            if self.is_measure:
                select.append({
                    "Measure": self._measure_expr(self.measure_col, self.meas_alias)["Measure"],
                    "Name": self._meas_ref(self.measure_col),
                })
            else:
                select.append({
                    "Aggregation": self._agg_expr(self.measure_col, self.meas_alias)["Aggregation"],
                    "Name": self._meas_ref(self.measure_col),
                })

        for f in self.extra_fields:
            if f and f != self.dim_col and f != self.measure_col:
                select.append({
                    "Column": self._col_expr(f, self.dim_alias)["Column"],
                    "Name": self._dim_ref(f),
                })

        if not select:
            select.append({
                "Column": self._col_expr("__placeholder__", self.dim_alias)["Column"],
                "Name": f"{self.dim_table}.__placeholder__",
            })

        return {"Version": 2, "From": from_clause, "Select": select, "OrderBy": []}

    # ── Projections / column properties / transforms ───────────────────────

    def build_projections(self) -> dict[str, list]:
        proj: dict[str, list] = {}
        role_x = self.roles.get("x")
        role_y = self.roles.get("y")
        role_legend = self.roles.get("legend")
        role_values = self.roles.get("values")

        if role_values:
            items = []
            if self.dim_col:
                items.append({"queryRef": self._dim_ref(self.dim_col)})
            if self.measure_col:
                items.append({"queryRef": self._meas_ref(self.measure_col), "active": True})
            for f in self.extra_fields:
                if f:
                    items.append({"queryRef": self._dim_ref(f)})
            if items:
                proj[role_values] = items
        else:
            if role_x and self.dim_col:
                proj[role_x] = [{"queryRef": self._dim_ref(self.dim_col)}]
            if role_y and self.measure_col:
                proj[role_y] = [{"queryRef": self._meas_ref(self.measure_col), "active": True}]
            if role_legend and self.legend_col:
                proj[role_legend] = [{"queryRef": self._legend_ref(self.legend_col)}]

        return proj

    def build_column_properties(self) -> dict:
        props: dict[str, dict] = {}
        if self.dim_col:
            props[self._dim_ref(self.dim_col)] = {"displayName": self.dim_col}
        if self.measure_col:
            label = self.measure_col if self.is_measure else f"Sum of {self.measure_col}"
            props[self._meas_ref(self.measure_col)] = {"displayName": label}
        if self.legend_col:
            props[self._legend_ref(self.legend_col)] = {"displayName": self.legend_col}
        for f in self.extra_fields:
            if f:
                props[self._dim_ref(f)] = {"displayName": f}
        return props

    def build_transforms(self) -> dict:
        selects: list[dict] = []
        if self.dim_col:
            selects.append({"Name": self._dim_ref(self.dim_col), "Restatement": self.dim_col, "Type": 1})
        if self.legend_col:
            selects.append({"Name": self._legend_ref(self.legend_col), "Restatement": self.legend_col, "Type": 1})
        if self.measure_col:
            label = self.measure_col if self.is_measure else f"Sum of {self.measure_col}"
            selects.append({"Name": self._meas_ref(self.measure_col), "Restatement": label, "Type": 260})
        for f in self.extra_fields:
            if f and f != self.dim_col and f != self.measure_col:
                selects.append({"Name": self._dim_ref(f), "Restatement": f, "Type": 1})

        role_manips: dict[str, list] = {}
        for role_key, col, ref_fn in [
            ("x",      self.dim_col,     self._dim_ref),
            ("y",      self.measure_col, self._meas_ref),
            ("legend", self.legend_col,  self._legend_ref),
        ]:
            pbi_role = self.roles.get(role_key)
            if pbi_role and col:
                role_manips[pbi_role] = [{"queryRef": ref_fn(col), "active": True}]

        if self.roles.get("values") and (self.dim_col or self.measure_col):
            vals = []
            for c, ref_fn in [(self.dim_col, self._dim_ref), (self.measure_col, self._meas_ref)]:
                if c:
                    vals.append({"queryRef": ref_fn(c)})
            for f in self.extra_fields:
                if f:
                    vals.append({"queryRef": self._dim_ref(f)})
            if vals:
                role_manips[self.roles["values"]] = vals

        return {"queryMetadata": {"Selects": selects}, "roleManipulations": role_manips}


# ---------------------------------------------------------------------------
# Single visual container builder
# ---------------------------------------------------------------------------

class VisualContainerBuilder:
    """Builds a single Power BI visual container dict from a VisualSpec dict.

    Uses :class:`SemanticResolver` for all field lookups so that:
    * Tableau datasource IDs are ignored as table hints
    * 'SalesAmount' resolves to the DAX measure 'Sum SalesAmount' via
      tableau_expression matching, not to the raw column
    * Dimension and measure fields can bind to different physical tables;
      the generated prototypeQuery includes both in its From clause
    """

    def __init__(
        self,
        model: "PBIXSemanticModel",
        resolver: Any = None,
        field_indexer: Any = None,
        strict_field_index: bool = False,
    ) -> None:
        self._model = model
        self._resolver = resolver  # SemanticResolver, may be None for legacy callers
        self._field_indexer = field_indexer
        self._strict_field_index = strict_field_index
        # Fallback index (used when no resolver is provided)
        self._fallback_table = model.tables[0].name if model.tables else "Table"

    def _resolve_via_resolver(self, name: str, table_hint: str = "", role_hint: str = "") -> Any | None:
        """Use SemanticResolver if available; otherwise None."""
        if self._resolver is None:
            return None
        return self._resolver.resolve_field(name, table_hint=table_hint, role_hint=role_hint)

    def _fallback_resolve(self, name: str) -> tuple[str, bool]:
        """Legacy fallback: case-insensitive column lookup on pbix_model."""
        key = str(name or "").strip().lower()
        for tbl in self._model.tables:
            for m in tbl.measures:
                if m.name.strip().lower() == key:
                    return tbl.name, True
            for col in tbl.columns:
                if col.name.strip().lower() == key:
                    return tbl.name, False
        return self._fallback_table, False

    def _binding_fields(
        self, visual: dict
    ) -> tuple[str, str, str | None, str | None, str, bool, str | None, str | None, str | None, str | None]:
        """Resolve (dim_table, meas_table, dim_col, measure_col, agg, is_measure, legend_col, dim_query_ref, meas_query_ref, legend_query_ref).

        Returns two separate table names so _QueryBuilder can build a correct
        multi-table From clause when the dimension and measure live in different
        physical entities.
        """
        binding = visual.get("data_binding") if isinstance(visual.get("data_binding"), dict) else {}
        axes = binding.get("axes") if isinstance(binding.get("axes"), dict) else {}
        measures_list = binding.get("measures") if isinstance(binding.get("measures"), list) else []
        agg = str(binding.get("aggregation") or "sum").strip().lower() or "sum"

        x_ax = axes.get("x") if isinstance(axes.get("x"), dict) else {}
        y_ax = axes.get("y") if isinstance(axes.get("y"), dict) else {}
        color_ax = axes.get("color") if isinstance(axes.get("color"), dict) else {}

        # After SemanticBinder runs, axis dicts already have resolved table+column/name.
        # These picks honour that.
        x_ref = str(x_ax.get("column") or x_ax.get("name") or "").strip()
        x_tbl = str(x_ax.get("table") or "").strip()
        y_ref = str(y_ax.get("column") or y_ax.get("name") or "").strip()
        y_tbl = str(y_ax.get("table") or "").strip()
        color_ref = str(color_ax.get("column") or color_ax.get("name") or "").strip() or None

        # Prefer explicit measures[] list for the measure field
        measure_override: str | None = None
        if measures_list:
            measure_override = str(measures_list[0].get("name") or "").strip() or None

        # ── Resolve each axis independently ──────────────────────────────
        def _resolve(ref: str, tbl: str, role: str) -> tuple[str, str, bool, str | None, str | None]:
            """Return (table, field_name, is_measure, query_ref, aggregation)."""
            if not ref:
                return self._fallback_table, ref, False, None, None
            if self._field_indexer is not None:
                indexed = self._field_indexer.resolve(ref, table_hint=tbl, role_hint=role, aggregation=agg)
                if indexed is not None:
                    return indexed.entity, indexed.property, indexed.is_measure, indexed.query_ref, indexed.aggregation
                if self._strict_field_index:
                    return self._fallback_table, ref, False, None, None
            rf = self._resolve_via_resolver(ref, table_hint=tbl, role_hint=role)
            if rf is not None:
                return rf.table, rf.display_name(), rf.is_measure, rf.ref(), None
            # Legacy fallback
            t, is_m = self._fallback_resolve(ref)
            return t, ref, is_m, f"{t}.{ref}", None

        x_table, x_name, x_is_meas, x_qref, _ = _resolve(x_ref, x_tbl, "dimension")
        y_table, y_name, y_is_meas, y_qref, y_agg = _resolve(y_ref, y_tbl, "measure")

        # The measure col / name comes from: explicit measures list → y axis → x (if measure)
        if measure_override:
            m_table, m_name, m_is_meas, m_qref, m_agg = _resolve(measure_override, "", "measure")
        elif y_ref:
            m_table, m_name, m_is_meas, m_qref, m_agg = y_table, y_name, y_is_meas, y_qref, y_agg
        elif x_is_meas:
            m_table, m_name, m_is_meas, m_qref, m_agg = x_table, x_name, x_is_meas, x_qref, None
        else:
            m_table = m_name = ""
            m_is_meas = False
            m_qref = None
            m_agg = None

        # Dimension col — x axis if it's not a measure
        if x_ref and not x_is_meas:
            dim_col: str | None = x_name
            dim_table = x_table
        elif x_ref and x_is_meas:
            # x resolves to a measure — treat it as the measure field
            if not m_name:
                m_table, m_name, m_is_meas, m_qref = x_table, x_name, True, x_qref
            dim_col = None
            dim_table = m_table
        else:
            dim_col = None
            dim_table = m_table or self._fallback_table

        meas_col: str | None = m_name or None
        meas_table = m_table or dim_table

        # Legend (color axis)
        legend_col: str | None = None
        if color_ref:
            cr_table, cr_name, _, legend_qref, _ = _resolve(color_ref, "", "dimension")
            legend_col = cr_name
        else:
            legend_qref = None

        if m_is_meas:
            agg = "none"
        elif m_agg and str(m_agg).strip().lower() != "none":
            agg = str(m_agg).strip().lower()

        if self._strict_field_index:
            if dim_col and not x_qref:
                dim_col = None
            if meas_col and not m_qref:
                meas_col = None
            if legend_col and not legend_qref:
                legend_col = None

        return dim_table, meas_table, dim_col, meas_col, agg, m_is_meas, legend_col, x_qref if dim_col else None, m_qref, legend_qref

    def _color_objects(self, fill_color: str | None, series_colors: list[str]) -> dict:
        """Build singleVisual.objects dict for color formatting."""
        obj: dict[str, Any] = {}
        if fill_color:
            hex_val = fill_color if fill_color.startswith("#") else f"#{fill_color}"
            obj["dataColors"] = [
                {"properties": {"fill": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_val}'"}}}}}}},
            ]
        if series_colors:
            sentiment = []
            for color in series_colors[:5]:
                hex_val = color if color.startswith("#") else f"#{color}"
                sentiment.append(
                    {"properties": {"dataPointColor": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_val}'"}}}}}}},
                )
            if sentiment:
                obj["dataPoint"] = sentiment
        return obj

    def build(
        self,
        visual: dict,
        fallback_x: int = 20,
        fallback_y: int = 20,
        fallback_w: int = 400,
        fallback_h: int = 280,
    ) -> dict | None:
        """Build a visual container or return None for unsupported / empty visuals."""
        logical_type = str(visual.get("type") or "table").strip().lower()
        pbi_type = _pbi_type(logical_type)

        # Resolve position
        pos = visual.get("position") if isinstance(visual.get("position"), dict) else {}
        raw_x = pos.get("x")
        raw_y = pos.get("y")
        raw_w = pos.get("width")
        raw_h = pos.get("height")

        # If values look like Tableau units (0–2000), scale; if PBI pixels (already large), keep
        def _coerce(val, fallback, scale_fn):
            if val is None:
                return fallback
            try:
                fval = float(val)
            except (TypeError, ValueError):
                return fallback
            # Heuristic: Tableau uses 0–1200; PBI uses 0–1280. If > 1280 it's PBI already.
            if fval > 1280:
                return int(fval)
            return scale_fn(fval)

        x = _coerce(raw_x, fallback_x, _scale_x)
        y = _coerce(raw_y, fallback_y, _scale_y)
        w = _coerce(raw_w, fallback_w, lambda v: _scale_w(v, fallback_w))
        h = _coerce(raw_h, fallback_h, lambda v: _scale_h(v, fallback_h))

        visual_id = str(visual.get("id") or visual.get("source_worksheet") or f"visual_{logical_type}")
        title = str(visual.get("title") or visual_id or "")

        # Image / textbox — no data binding needed
        if pbi_type in {"image", "textbox"}:
            return self._textbox_container(visual_id, title, x, y, w, h)

        # Resolve data bindings
        dim_table, meas_table, dim_col, meas_col, agg, is_meas, legend_col, dim_qref, meas_qref, legend_qref = self._binding_fields(visual)

        # Build query and projections
        qb = _QueryBuilder(
            dim_table=dim_table,
            meas_table=meas_table,
            dim_col=dim_col,
            measure_col=meas_col,
            pbi_visual_type=pbi_type,
            is_measure=is_meas,
            agg=agg,
            legend_col=legend_col,
            dim_query_ref=dim_qref,
            meas_query_ref=meas_qref,
            legend_query_ref=legend_qref,
        )

        query = qb.build_query()
        projections = qb.build_projections()
        col_props = qb.build_column_properties()
        transforms = qb.build_transforms()

        # Color objects
        fill = visual.get("fill_color") or None
        series = list(visual.get("series_colors") or [])
        color_objs = self._color_objects(fill, series) if (fill or series) else {}

        config: dict[str, Any] = {
            "name": f"{pbi_type}_{_sanitize_name(visual_id)}",
            "layouts": [
                {"id": 0, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0}}
            ],
            "singleVisual": {
                "visualType": pbi_type,
                "projections": projections,
                "prototypeQuery": query,
                "columnProperties": col_props,
            },
        }
        if title:
            config["singleVisual"]["title"] = {"show": {"expr": {"Literal": {"Value": "true"}}}}
            config["singleVisual"]["vcObjects"] = {
                "title": [{"properties": {"text": {"expr": {"Literal": {"Value": f"'{title.replace(chr(39), chr(39)+chr(39))}'"}}}, "show": {"expr": {"Literal": {"Value": "true"}}}}}]
            }
        if color_objs:
            config["singleVisual"]["objects"] = color_objs

        return {
            "x": x,
            "y": y,
            "z": 0,
            "width": w,
            "height": h,
            "config": json.dumps(config, separators=(",", ":")),
            "filters": "[]",
            "query": json.dumps(query, separators=(",", ":")),
            "dataTransforms": json.dumps(transforms, separators=(",", ":")),
        }

    # ── Slicer ──────────────────────────────────────────────────────────────

    def build_slicer(
        self,
        filter_field: str,
        filter_table: str | None,
        x: int,
        y: int,
        w: int = 200,
        h: int = 80,
        idx: int = 0,
    ) -> dict:
        """Build a slicer visual container for a filter field."""
        table = filter_table or (self._model.tables[0].name if self._model.tables else "Table")
        alias = _safe_alias(table)
        ref = f"{table}.{filter_field}"
        query = {
            "Version": 2,
            "From": [{"Name": alias, "Entity": table, "Type": 0}],
            "Select": [
                {
                    "Column": {
                        "Expression": {"SourceRef": {"Source": alias}},
                        "Property": filter_field,
                    },
                    "Name": ref,
                }
            ],
        }
        config = {
            "name": f"slicer_{idx}_{_sanitize_name(filter_field)}",
            "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 200 + idx}}],
            "singleVisual": {
                "visualType": "slicer",
                "projections": {"Values": [{"queryRef": ref}]},
                "prototypeQuery": query,
                "columnProperties": {ref: {"displayName": filter_field}},
                "objects": {
                    "general": [{"properties": {"outlineColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#000000'"}}}}}, "outlineWeight": {"expr": {"Literal": {"Value": "1"}}}, "orientation": {"expr": {"Literal": {"Value": "'0'"}}}, "filter": {}, "selfFilterEnabled": {"expr": {"Literal": {"Value": "false"}}}}}]
                },
            },
        }
        return {
            "x": x, "y": y, "z": 0, "width": w, "height": h,
            "config": json.dumps(config, separators=(",", ":")),
            "filters": "[]",
            "query": json.dumps(query, separators=(",", ":")),
            "dataTransforms": "{}",
        }

    # ── Textbox ─────────────────────────────────────────────────────────────

    def _textbox_container(self, visual_id: str, text: str, x: int, y: int, w: int, h: int) -> dict:
        safe_text = text.replace("'", "''")
        config = {
            "name": f"textbox_{_sanitize_name(visual_id)}",
            "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0}}],
            "singleVisual": {
                "visualType": "textbox",
                "objects": {
                    "general": [{"properties": {"paragraphs": {"expr": {"Literal": {"Value": json.dumps(text)}}}}}]
                },
            },
        }
        return {
            "x": x, "y": y, "z": 0, "width": w, "height": h,
            "config": json.dumps(config, separators=(",", ":")),
            "filters": "[]",
            "query": "{}",
            "dataTransforms": "{}",
        }


# ---------------------------------------------------------------------------
# Auto-placement grid
# ---------------------------------------------------------------------------

class _AutoPlacer:
    """Grid-based auto placement for visuals that lack explicit positions."""

    def __init__(self, cols: int = 2, col_w: int = 580, row_h: int = 300, margin: int = 20) -> None:
        self._cols = cols
        self._col_w = col_w
        self._row_h = row_h
        self._margin = margin
        self._count = 0

    def next(self, w: int | None = None, h: int | None = None) -> tuple[int, int, int, int]:
        col = self._count % self._cols
        row = self._count // self._cols
        x = self._margin + col * (self._col_w + self._margin)
        y = self._margin + row * (self._row_h + self._margin)
        self._count += 1
        return x, y, w or self._col_w, h or self._row_h


# ---------------------------------------------------------------------------
# Full report builder
# ---------------------------------------------------------------------------

class PBIXReportBuilder:
    """Convert AbstractSpec dict + PBIXSemanticModel → Report/Layout JSON.

    Usage::

        layout = PBIXReportBuilder().build(abstract_spec_dict, pbix_model)
    """

    def build(
        self,
        spec: dict[str, Any],
        model: "PBIXSemanticModel",
        field_indexer: Any = None,
        strict_field_index: bool = False,
        report_config_version: str = "5.67",
        theme_name: str = "CY25SU10",
    ) -> dict[str, Any]:
        """Return the full Report/Layout payload."""
        from engine.creation.semantic_resolver import SemanticResolver

        dashboard = spec.get("dashboard_spec") if isinstance(spec.get("dashboard_spec"), dict) else {}
        pages_raw = dashboard.get("pages") if isinstance(dashboard.get("pages"), list) else []
        global_filters = dashboard.get("global_filters") if isinstance(dashboard.get("global_filters"), list) else []

        # Build SemanticResolver from the spec's semantic model (preferred) so
        # the visual builder has access to tableau_expression aliases and exact
        # lineage, even when the base PBIX was in ABF format.
        semantic_model = spec.get("semantic_model") if isinstance(spec.get("semantic_model"), dict) else {}
        resolver = SemanticResolver(semantic_model) if semantic_model else None

        vcb = VisualContainerBuilder(
            model,
            resolver=resolver,
            field_indexer=field_indexer,
            strict_field_index=strict_field_index,
        )
        sections: list[dict] = []

        for p_idx, page in enumerate(pages_raw):
            if not isinstance(page, dict):
                continue
            page_name = str(page.get("name") or page.get("id") or f"Page {p_idx + 1}")
            visuals_raw = page.get("visuals") if isinstance(page.get("visuals"), list) else []
            page_filters = page.get("filters") if isinstance(page.get("filters"), list) else []

            containers: list[dict] = []
            placer = _AutoPlacer(cols=2, col_w=580, row_h=300)

            for v_idx, visual in enumerate(visuals_raw):
                if not isinstance(visual, dict):
                    continue

                pos = visual.get("position") if isinstance(visual.get("position"), dict) else {}
                has_pos = all(pos.get(k) is not None for k in ("x", "y", "width", "height"))

                if has_pos:
                    container = vcb.build(visual)
                else:
                    fx, fy, fw, fh = placer.next()
                    container = vcb.build(visual, fallback_x=fx, fallback_y=fy, fallback_w=fw, fallback_h=fh)

                if container is not None:
                    containers.append(container)

            # Add slicers for page-level filters
            slicer_y = _PAGE_H - 100
            for f_idx, filt in enumerate(page_filters):
                if not isinstance(filt, dict):
                    continue
                field_name = str(filt.get("field") or filt.get("column") or "").strip()
                table_hint = str(filt.get("table") or "").strip() or None
                if field_name:
                    slicer_x = 20 + f_idx * 220
                    containers.append(vcb.build_slicer(field_name, table_hint, slicer_x, slicer_y, w=200, h=80, idx=f_idx))

            # Add slicers for global filters on first page only
            if p_idx == 0:
                for f_idx, filt in enumerate(global_filters):
                    if not isinstance(filt, dict):
                        continue
                    field_name = str(filt.get("field") or filt.get("column") or "").strip()
                    table_hint = str(filt.get("table") or "").strip() or None
                    if field_name:
                        slicer_x = 20 + (len(page_filters) + f_idx) * 220
                        containers.append(vcb.build_slicer(field_name, table_hint, slicer_x, slicer_y, w=200, h=80, idx=len(page_filters) + f_idx))

            sections.append({
                "id": p_idx,
                "name": f"Section{p_idx + 1}",
                "displayName": page_name,
                "filters": "[]",
                "ordinal": p_idx,
                "visualContainers": containers,
                "config": json.dumps({"defaultFilters": [], "visibility": 0}, separators=(",", ":")),
                "width": _PAGE_W,
                "height": _PAGE_H,
                "displayOption": 0,
            })

        # Ensure at least one page
        if not sections:
            sections = [_empty_section()]

        config = {
            "version": report_config_version,
            "themeCollection": {
                "baseTheme": {
                    "name": theme_name,
                    "version": report_config_version,
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


def _empty_section() -> dict:
    return {
        "id": 0,
        "name": "Section1",
        "displayName": "Page 1",
        "filters": "[]",
        "ordinal": 0,
        "visualContainers": [],
        "config": json.dumps({"defaultFilters": [], "visibility": 0}, separators=(",", ":")),
        "width": _PAGE_W,
        "height": _PAGE_H,
        "displayOption": 0,
    }
