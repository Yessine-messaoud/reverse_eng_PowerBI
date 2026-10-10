from __future__ import annotations

from typing import Any


def generate_layout(visuals: list[dict[str, Any]], columns: int = 4) -> dict[str, Any]:
    """Position dashboard elements on a simple responsive grid."""
    layout: list[dict[str, Any]] = []
    cursor_y = 0

    for index, visual in enumerate(visuals):
        visual_type = str(visual.get("type") or "chart")
        if visual_type == "kpi":
            cell = {"element": f"visual_{index}", "type": visual_type, "x": (index % columns), "y": 0, "w": 1, "h": 1}
        elif visual_type == "line_chart":
            cell = {"element": f"visual_{index}", "type": visual_type, "x": 0, "y": max(1, cursor_y), "w": 4, "h": 2}
            cursor_y = cell["y"] + cell["h"]
        else:
            x = 0 if len([item for item in layout if item["y"] == cursor_y]) == 0 else 2
            cell = {"element": f"visual_{index}", "type": visual_type, "x": x, "y": max(1, cursor_y), "w": 2, "h": 2}
            if x == 2:
                cursor_y = cell["y"] + cell["h"]
        layout.append(cell)

    return {"layout": layout, "grid_columns": columns}
