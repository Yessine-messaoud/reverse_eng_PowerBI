from __future__ import annotations

from typing import Any


def select_visuals(insights: dict[str, Any]) -> list[dict[str, Any]]:
    """Map an analysis plan to a limited, non-redundant set of visuals."""
    visuals: list[dict[str, Any]] = []
    plan = insights.get("analysis_plan", []) if isinstance(insights.get("analysis_plan"), list) else []

    for item in plan:
        if not isinstance(item, dict):
            continue
        analysis_type = str(item.get("type") or "")
        if analysis_type == "trend":
            visuals.append({"type": "line_chart", "x": item.get("x"), "y": item.get("y")})
        elif analysis_type == "comparison":
            visuals.append({"type": "bar_chart", "x": item.get("group"), "y": item.get("metric")})
        elif analysis_type == "single_metric":
            visuals.append({"type": "kpi", "metric": item.get("metric")})

    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for visual in visuals:
        key = tuple(sorted(visual.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(visual)
        if len(unique) >= 5:
            break
    return unique
