"""
semantic_binder.py — Semantic binding validator and healer for PBIX generation.

Given an AbstractSpec dict (semantic_model + dashboard_spec), this module
verifies that every field reference in every visual's data_binding resolves
to a real column or measure in the semantic model.

Uses SemanticResolver for all field lookups — no fuzzy name matching.

Healing rules
-------------
1. Exact match         → mark "ok", update table/column in the axis dict
2. Fallback available  → mark "healed", substitute closest valid field
3. No candidate        → mark "error", leave binding intact for inspection

Synthetic table IDs (Tableau federated.xxx datasource names) in axis["table"]
are always replaced with the resolved physical entity name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Binding report
# ---------------------------------------------------------------------------

@dataclass
class BindingIssue:
    visual_id: str
    axis: str              # "x", "y", "color", "measure[0]", etc.
    original_ref: str      # what was in the spec
    resolved_ref: str      # what we resolved it to
    severity: str          # "ok" | "healed" | "error"
    message: str


@dataclass
class BindingReport:
    total_visuals: int = 0
    total_bindings_checked: int = 0
    issues: list[BindingIssue] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "ok")

    @property
    def healed_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "healed")

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_visuals": self.total_visuals,
            "total_bindings_checked": self.total_bindings_checked,
            "ok": self.ok_count,
            "healed": self.healed_count,
            "errors": self.error_count,
            "issues": [
                {
                    "visual_id": i.visual_id,
                    "axis": i.axis,
                    "original_ref": i.original_ref,
                    "resolved_ref": i.resolved_ref,
                    "severity": i.severity,
                    "message": i.message,
                }
                for i in self.issues
                if i.severity != "ok"
            ],
        }


# ---------------------------------------------------------------------------
# Binder
# ---------------------------------------------------------------------------

class SemanticBinder:
    """Validate and heal field bindings in an AbstractSpec dict.

    Uses :class:`SemanticResolver` for all lookups — no fuzzy matching.

    Usage::

        binder = SemanticBinder()
        healed_spec, report = binder.bind(abstract_spec)
    """

    def bind(
        self,
        spec: dict[str, Any],
    ) -> tuple[dict[str, Any], BindingReport]:
        """Return (healed_spec, report). The original *spec* is not mutated."""
        import copy
        from engine.creation.semantic_resolver import SemanticResolver

        spec = copy.deepcopy(spec)
        report = BindingReport()

        semantic = spec.get("semantic_model") if isinstance(spec.get("semantic_model"), dict) else {}
        resolver = SemanticResolver(semantic)

        dashboard = spec.get("dashboard_spec") if isinstance(spec.get("dashboard_spec"), dict) else {}
        pages = dashboard.get("pages") if isinstance(dashboard.get("pages"), list) else []

        for page in pages:
            if not isinstance(page, dict):
                continue
            visuals = page.get("visuals") if isinstance(page.get("visuals"), list) else []
            report.total_visuals += len(visuals)
            for visual in visuals:
                if not isinstance(visual, dict):
                    continue
                self._bind_visual(visual, resolver, report)

        return spec, report

    # ── Internal ───────────────────────────────────────────────────────────

    def _bind_visual(
        self,
        visual: dict[str, Any],
        resolver: Any,  # SemanticResolver
        report: BindingReport,
    ) -> None:
        vid = str(visual.get("id") or visual.get("source_worksheet") or "unknown")
        binding = visual.get("data_binding")
        if not isinstance(binding, dict):
            return

        axes = binding.get("axes") if isinstance(binding.get("axes"), dict) else {}
        measures = binding.get("measures") if isinstance(binding.get("measures"), list) else []

        # ── Bind axes ────────────────────────────────────────────────────
        for axis_key, axis_val in list(axes.items()):
            if not isinstance(axis_val, dict):
                continue
            report.total_bindings_checked += 1

            ref_name = str(axis_val.get("column") or axis_val.get("name") or "").strip()
            table_hint = str(axis_val.get("table") or "").strip()
            role_hint = "measure" if axis_key == "y" else "dimension" if axis_key == "x" else ""

            if not ref_name:
                continue

            resolved = resolver.resolve_field(ref_name, table_hint=table_hint, role_hint=role_hint)
            if resolved:
                # Always update table — overwrite synthetic/Tableau datasource IDs
                if resolver.is_synthetic_id(table_hint) or not table_hint or table_hint != resolved.table:
                    axis_val["table"] = resolved.table

                if resolved.is_measure:
                    # Bind to DAX measure: use 'name' key, not 'column'
                    axis_val.pop("column", None)
                    axis_val["name"] = resolved.measure_name
                else:
                    # Bind to column
                    axis_val["column"] = resolved.column
                    axis_val.pop("name", None)

                report.issues.append(BindingIssue(
                    visual_id=vid, axis=axis_key,
                    original_ref=ref_name, resolved_ref=resolved.display_name(),
                    severity="ok",
                    message=(
                        f"Resolved '{ref_name}' -> '{resolved.table}'.'{resolved.display_name()}' "
                        f"[{resolved.resolution_path}]"
                    ),
                ))
            else:
                fallback = self._fallback(axis_key, resolver)
                if fallback:
                    _log.warning(
                        "visual=%s axis=%s unresolved ref='%s' -> healed to '%s'",
                        vid, axis_key, ref_name, fallback.display_name(),
                    )
                    axis_val["table"] = fallback.table
                    if fallback.is_measure:
                        axis_val.pop("column", None)
                        axis_val["name"] = fallback.measure_name
                    else:
                        axis_val["column"] = fallback.column
                        axis_val.pop("name", None)
                    report.issues.append(BindingIssue(
                        visual_id=vid, axis=axis_key,
                        original_ref=ref_name, resolved_ref=fallback.display_name(),
                        severity="healed",
                        message=f"'{ref_name}' not found — healed to fallback '{fallback.display_name()}'",
                    ))
                else:
                    report.issues.append(BindingIssue(
                        visual_id=vid, axis=axis_key,
                        original_ref=ref_name, resolved_ref="",
                        severity="error",
                        message=f"'{ref_name}' unresolvable and no fallback available",
                    ))

        # ── Bind explicit measure list ────────────────────────────────────
        for m_idx, meas_ref in enumerate(measures):
            if not isinstance(meas_ref, dict):
                continue
            report.total_bindings_checked += 1
            m_name = str(meas_ref.get("name") or "").strip()
            if not m_name:
                continue

            # Try measure resolution first (exact + tableau_expression), then column
            resolved = resolver.resolve_measure(m_name) or resolver.resolve_field(m_name, role_hint="measure")
            if resolved:
                meas_ref["name"] = resolved.display_name()
                report.issues.append(BindingIssue(
                    visual_id=vid, axis=f"measure[{m_idx}]",
                    original_ref=m_name, resolved_ref=resolved.display_name(),
                    severity="ok",
                    message=f"Measure '{m_name}' -> '{resolved.display_name()}' [{resolved.resolution_path}]",
                ))
            else:
                report.issues.append(BindingIssue(
                    visual_id=vid, axis=f"measure[{m_idx}]",
                    original_ref=m_name, resolved_ref="",
                    severity="error",
                    message=f"Measure '{m_name}' not found in semantic model",
                ))

    @staticmethod
    def _fallback(axis_key: str, resolver: Any) -> Any:
        """Last-resort fallback when resolution fails."""
        if axis_key == "y":
            return resolver.first_measure()
        if axis_key == "x":
            return resolver.first_dimension_column()
        return resolver.first_measure()
