from __future__ import annotations

from typing import Any


def _build_title(prompt_info: dict[str, Any]) -> str:
    subject = str(prompt_info.get("requested_subject") or "dashboard").strip()
    subject = subject[:1].upper() + subject[1:] if subject else "Dashboard"
    return f"{subject} Dashboard" if "dashboard" not in subject.lower() else subject.title()


def build_dashboard_model(
    visuals: list[dict[str, Any]],
    layout: dict[str, Any],
    prompt_info: dict[str, Any],
    schema_info: dict[str, Any],
) -> dict[str, Any]:
    """Build a unified dashboard representation."""
    layout_map = {
        item["element"]: item
        for item in (layout.get("layout", []) if isinstance(layout.get("layout"), list) else [])
        if isinstance(item, dict)
    }
    elements: list[dict[str, Any]] = []

    for index, visual in enumerate(visuals):
        position = layout_map.get(f"visual_{index}", {"x": 0, "y": index, "w": 2, "h": 2})
        visual_type = str(visual.get("type") or "chart")
        subtype = "line" if visual_type == "line_chart" else "bar" if visual_type == "bar_chart" else "kpi"
        data = {key: value for key, value in visual.items() if key != "type"}
        elements.append(
            {
                "id": f"element_{index + 1}",
                "type": "metric" if visual_type == "kpi" else "chart",
                "subtype": subtype,
                "data": data,
                "position": {"x": position["x"], "y": position["y"], "w": position["w"], "h": position["h"]},
            }
        )

    return {
        "title": _build_title(prompt_info),
        "description": f"Auto-generated dashboard for {prompt_info.get('intent', 'analysis')}.",
        "metadata": {
            "primary_table": schema_info.get("primary_table", ""),
            "measures": schema_info.get("measures", []),
            "dimensions": schema_info.get("dimensions", []),
        },
        "elements": elements,
    }
