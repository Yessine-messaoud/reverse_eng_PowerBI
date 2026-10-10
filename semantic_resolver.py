"""
semantic_resolver.py — Lineage-based semantic field resolver for PBIX generation.

Builds an exact lookup index from the logical semantic model and resolves
visual field references to their physical table / column / measure bindings.

Resolution priority (no fuzzy matching anywhere):
  1  Exact DAX measure name
  2  Tableau expression alias         e.g. 'SalesAmount' → 'Sum SalesAmount'
  3  Column in table_hint             only when table_hint is a real entity name
  4  Measure-role column in fact table
  5  Column in entity matching structural_hint
  6  First exact column match across all entities
  → None                              never falls through to fuzzy

Design rules
------------
* Index keys are normalised: lower-case, spaces/underscores removed.
* Synthetic / Tableau datasource IDs (federated.xxx, GUIDs) are detected and
  ignored as table hints.
* Multi-table visuals are explicitly supported: dim and measure can bind to
  different physical tables; callers receive both table names separately.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

# Detects Tableau datasource IDs and other synthetic identifiers that are NOT
# real physical table names.
_SYNTHETIC_RE = re.compile(
    r"^federated\."       # Tableau federated datasource prefix
    r"|^\{[0-9A-Fa-f\-]{32,}\}$"  # GUID-like {xxxxxxxx-...}
    r"|^[0-9a-f]{20,}$"  # Raw long hex strings
    r"|^\d+\w{10,}$",    # Numeric-prefixed opaque IDs
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedField:
    """Exact physical binding for one visual field reference."""

    table: str           # Physical table (entity) name
    column: str          # Physical column name (or measure host column)
    is_measure: bool     # True → reference a DAX Measure; False → Column
    measure_name: str    # DAX measure name when is_measure=True; "" otherwise
    role: str            # "measure" | "dimension"
    structural_hint: str # "fact" | "dimension" | ""
    resolution_path: str # Diagnostic: how this was resolved

    def display_name(self) -> str:
        """The name to use in the query / projection (measure or column)."""
        return self.measure_name if self.is_measure else self.column

    def ref(self) -> str:
        """Canonical query reference string: 'Table.Field'."""
        return f"{self.table}.{self.display_name()}"


# ---------------------------------------------------------------------------
# Internal records
# ---------------------------------------------------------------------------

@dataclass
class _ColumnRecord:
    table: str
    name: str
    pbi_type: str
    role: str            # "measure" | "dimension"
    structural_hint: str # entity's structural hint


@dataclass
class _MeasureRecord:
    name: str
    table: str           # host entity (fact table or source_columns table)
    expression: str
    source_table: str    # from source_columns[0].table if present
    source_column: str   # from source_columns[0].column if present
    tableau_expression: str  # original Tableau field name (alias for lookup)


# ---------------------------------------------------------------------------
# SemanticResolver
# ---------------------------------------------------------------------------

class SemanticResolver:
    """Exact, lineage-based field resolver for PBIX visual binding.

    Build once per spec; call :meth:`resolve_field` for each visual axis or
    measure reference.

    Example::

        resolver = SemanticResolver(spec["semantic_model"])

        # Resolves 'SalesAmount' via tableau_expression → 'Sum SalesAmount' DAX measure
        f = resolver.resolve_field("SalesAmount", role_hint="measure")
        # f.is_measure == True, f.measure_name == 'Sum SalesAmount', f.table == 'FactInternetSales'

        # Resolves column from a dimension table, ignoring synthetic table hint
        f = resolver.resolve_field(
            "SalesTerritoryCountry",
            table_hint="federated.1v3o2r30w2rgsv1aei9is1e9duvo",
        )
        # f.is_measure == False, f.table == 'DimSalesTerritory'
    """

    def __init__(self, semantic_model: dict[str, Any]) -> None:
        self._fact_tables: list[str] = []
        self._entity_hints: dict[str, str] = {}  # entity_name → structural_hint

        # column_name_lower → ordered list[_ColumnRecord] (fact entries first)
        self._col_index: dict[str, list[_ColumnRecord]] = {}

        # measure_name_lower → _MeasureRecord
        self._measure_index: dict[str, _MeasureRecord] = {}

        # tableau_expression_lower → _MeasureRecord  (alias lookup)
        self._tableau_index: dict[str, _MeasureRecord] = {}

        self._build(semantic_model)

    # ── Index construction ─────────────────────────────────────────────────

    def _build(self, sm: dict[str, Any]) -> None:
        entities: list[dict] = sm.get("entities", []) if isinstance(sm.get("entities"), list) else []
        explicit_fact = str(sm.get("fact_table") or "").strip()

        for entity in entities:
            if not isinstance(entity, dict):
                continue
            ename = str(entity.get("name") or entity.get("id") or "").strip()
            if not ename:
                continue
            hint = str(entity.get("structural_hint") or "").strip().lower()
            self._entity_hints[ename] = hint
            if hint == "fact" or ename == explicit_fact:
                if ename not in self._fact_tables:
                    self._fact_tables.append(ename)

            for col in (entity.get("columns") or []):
                if not isinstance(col, dict):
                    continue
                cname = str(col.get("name") or "").strip()
                if not cname:
                    continue
                rec = _ColumnRecord(
                    table=ename,
                    name=cname,
                    pbi_type=str(col.get("pbi_type") or col.get("type") or "string"),
                    role=str(col.get("role") or "dimension").lower(),
                    structural_hint=hint,
                )
                key = self._norm(cname)
                lst = self._col_index.setdefault(key, [])
                # Fact-table columns go first in the list
                if hint == "fact" or ename == explicit_fact:
                    lst.insert(0, rec)
                else:
                    lst.append(rec)

        measures: list[dict] = sm.get("measures", []) if isinstance(sm.get("measures"), list) else []
        default_host = self._fact_tables[0] if self._fact_tables else explicit_fact

        for m in measures:
            if not isinstance(m, dict):
                continue
            mname = str(m.get("name") or "").strip()
            if not mname:
                continue
            src_cols: list = m.get("source_columns") or []
            src_table = src_col = ""
            if src_cols and isinstance(src_cols[0], dict):
                src_table = str(src_cols[0].get("table") or "")
                src_col = str(src_cols[0].get("column") or "")
            host = src_table or default_host
            tableau_expr = str(m.get("tableau_expression") or "").strip()

            rec = _MeasureRecord(
                name=mname,
                table=host,
                expression=str(m.get("expression") or ""),
                source_table=src_table,
                source_column=src_col,
                tableau_expression=tableau_expr,
            )
            self._measure_index[self._norm(mname)] = rec
            if tableau_expr:
                tex_key = self._norm(tableau_expr)
                if tex_key not in self._tableau_index:
                    self._tableau_index[tex_key] = rec

    # ── Public helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _norm(name: str) -> str:
        """Normalise name for exact case-insensitive lookup."""
        return re.sub(r"[\s_]", "", str(name or "").strip().lower())

    @staticmethod
    def is_synthetic_id(name: str) -> bool:
        """Return True for Tableau datasource IDs, GUIDs, and other opaque names."""
        if not name:
            return False
        return bool(_SYNTHETIC_RE.match(str(name)))

    def get_fact_tables(self) -> list[str]:
        return list(self._fact_tables)

    def get_entity_hint(self, table: str) -> str:
        return self._entity_hints.get(table, "")

    def known_entity(self, name: str) -> bool:
        """True if *name* is a known entity (table) in the semantic model."""
        return name in self._entity_hints

    # ── Resolution ─────────────────────────────────────────────────────────

    def resolve_field(
        self,
        ref: str,
        table_hint: str = "",
        role_hint: str = "",
    ) -> ResolvedField | None:
        """Resolve *ref* to an exact physical binding.

        Parameters
        ----------
        ref:        The field name from the visual spec (column, measure, or alias).
        table_hint: The table name from the spec's axis dict — may be a Tableau
                    datasource ID (will be ignored).
        role_hint:  Optional "measure" or "dimension" to break ties.
        """
        if not ref:
            return None
        key = self._norm(ref)

        # 1. Exact DAX measure name
        if key in self._measure_index:
            rec = self._measure_index[key]
            return ResolvedField(
                table=rec.table,
                column=rec.source_column or ref,
                is_measure=True,
                measure_name=rec.name,
                role="measure",
                structural_hint=self._entity_hints.get(rec.table, ""),
                resolution_path="exact_measure",
            )

        # 2. Tableau expression alias  (e.g. 'SalesAmount' → 'Sum SalesAmount')
        if key in self._tableau_index:
            rec = self._tableau_index[key]
            return ResolvedField(
                table=rec.table,
                column=rec.source_column or ref,
                is_measure=True,
                measure_name=rec.name,
                role="measure",
                structural_hint=self._entity_hints.get(rec.table, ""),
                resolution_path="tableau_expression",
            )

        # Steps 3-6 work from the column index
        candidates = self._col_index.get(key)
        if not candidates:
            _log.debug("resolve_field: '%s' not found in any index", ref)
            return None

        # 3. Column in hinted table (only when hint is a real entity, not a synthetic ID)
        if table_hint and not self.is_synthetic_id(table_hint) and self.known_entity(table_hint):
            for rec in candidates:
                if rec.table == table_hint:
                    return ResolvedField(
                        table=rec.table, column=rec.name,
                        is_measure=False, measure_name="",
                        role=rec.role, structural_hint=rec.structural_hint,
                        resolution_path="column_table_hint",
                    )

        # 4. Measure-role column in a fact table
        if role_hint == "measure" or not role_hint:
            for rec in candidates:
                if rec.role == "measure" and rec.structural_hint == "fact":
                    return ResolvedField(
                        table=rec.table, column=rec.name,
                        is_measure=False, measure_name="",
                        role=rec.role, structural_hint=rec.structural_hint,
                        resolution_path="measure_column_fact",
                    )

        # 5. Column in a fact entity (any role)
        for rec in candidates:
            if rec.structural_hint == "fact":
                return ResolvedField(
                    table=rec.table, column=rec.name,
                    is_measure=False, measure_name="",
                    role=rec.role, structural_hint=rec.structural_hint,
                    resolution_path="column_fact",
                )

        # 6. First match in any entity
        rec = candidates[0]
        return ResolvedField(
            table=rec.table, column=rec.name,
            is_measure=False, measure_name="",
            role=rec.role, structural_hint=rec.structural_hint,
            resolution_path="column_any",
        )

    def resolve_column(self, name: str, table_hint: str = "") -> ResolvedField | None:
        """Resolve *name* as a column only (skips measure index)."""
        if not name:
            return None
        key = self._norm(name)
        candidates = self._col_index.get(key)
        if not candidates:
            return None
        if table_hint and not self.is_synthetic_id(table_hint) and self.known_entity(table_hint):
            for rec in candidates:
                if rec.table == table_hint:
                    return ResolvedField(
                        table=rec.table, column=rec.name,
                        is_measure=False, measure_name="",
                        role=rec.role, structural_hint=rec.structural_hint,
                        resolution_path="exact_column",
                    )
        rec = candidates[0]
        return ResolvedField(
            table=rec.table, column=rec.name,
            is_measure=False, measure_name="",
            role=rec.role, structural_hint=rec.structural_hint,
            resolution_path="exact_column_any",
        )

    def resolve_measure(self, name: str) -> ResolvedField | None:
        """Resolve *name* as a measure (exact name or tableau expression)."""
        if not name:
            return None
        key = self._norm(name)
        for index in (self._measure_index, self._tableau_index):
            if key in index:
                rec = index[key]
                return ResolvedField(
                    table=rec.table, column=rec.source_column or name,
                    is_measure=True, measure_name=rec.name,
                    role="measure",
                    structural_hint=self._entity_hints.get(rec.table, ""),
                    resolution_path="exact_measure" if index is self._measure_index else "tableau_expression",
                )
        return None

    def first_measure(self) -> ResolvedField | None:
        """Return the first declared measure, or None."""
        for rec in self._measure_index.values():
            return ResolvedField(
                table=rec.table, column=rec.source_column or rec.name,
                is_measure=True, measure_name=rec.name,
                role="measure",
                structural_hint=self._entity_hints.get(rec.table, ""),
                resolution_path="fallback_first_measure",
            )
        return None

    def first_dimension_column(self) -> ResolvedField | None:
        """Return the first dimension column from any entity."""
        for candidates in self._col_index.values():
            for rec in candidates:
                if rec.role == "dimension" and rec.pbi_type in ("text", "string", "dateTime"):
                    return ResolvedField(
                        table=rec.table, column=rec.name,
                        is_measure=False, measure_name="",
                        role=rec.role, structural_hint=rec.structural_hint,
                        resolution_path="fallback_first_dim",
                    )
        return None
