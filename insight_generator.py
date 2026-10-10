from __future__ import annotations

from typing import Any


def generate_insights(schema_info: dict[str, Any], prompt_info: dict[str, Any]) -> dict[str, Any]:
    """Combine schema analysis and prompt interpretation into an analysis plan."""
    metrics = prompt_info.get("metrics", []) if isinstance(prompt_info.get("metrics"), list) else []
    group_by = prompt_info.get("group_by", []) if isinstance(prompt_info.get("group_by"), list) else []
    time_field = prompt_info.get("time")
    plan: list[dict[str, Any]] = []

    for metric in metrics[:2]:
        if time_field:
            plan.append({"type": "trend", "x": time_field, "y": metric})
        for dimension in group_by[:2]:
            if dimension != time_field:
                plan.append({"type": "comparison", "group": dimension, "metric": metric})

    if not plan and metrics:
        plan.append({"type": "single_metric", "metric": metrics[0]})

    seen: set[tuple[Any, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for item in plan:
        key = tuple(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return {"analysis_plan": deduped}
