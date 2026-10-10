from __future__ import annotations

import re
from typing import Any


def _match_columns(text: str, candidates: list[str]) -> list[str]:
    lowered = text.lower()
    return [candidate for candidate in candidates if candidate.lower() in lowered]


def interpret_prompt(user_prompt: str, schema_info: dict[str, Any]) -> dict[str, Any]:
    """Extract a rule-based dashboard intent from a user prompt."""
    prompt = str(user_prompt or "").strip()
    lowered = prompt.lower()
    measures = schema_info.get("measures", []) if isinstance(schema_info.get("measures"), list) else []
    dimensions = schema_info.get("dimensions", []) if isinstance(schema_info.get("dimensions"), list) else []
    time_fields = schema_info.get("time_fields", []) if isinstance(schema_info.get("time_fields"), list) else []

    intent = "summary"
    if any(token in lowered for token in ["over time", "trend", "evolution", "timeline"]):
        intent = "trend_analysis"
    elif any(token in lowered for token in ["compare", "comparison", "by ", "versus", "vs"]):
        intent = "comparison"

    target_metrics = _match_columns(lowered, measures)
    grouping_fields = _match_columns(lowered, dimensions)
    time_field = next((field for field in time_fields if field.lower() in lowered), None)

    if not time_field and intent == "trend_analysis" and time_fields:
        time_field = time_fields[0]

    if not target_metrics and measures:
        target_metrics = [measures[0]]

    group_by = [field for field in grouping_fields if field not in target_metrics]
    if not group_by and dimensions:
        preferred = [field for field in dimensions if field not in (time_fields or [])]
        if preferred:
            group_by = [preferred[0]]

    title_match = re.search(r"(dashboard|report)\s+(?:for|about)?\s*(.+)", lowered)
    requested_subject = title_match.group(2).strip() if title_match else prompt

    return {
        "intent": intent,
        "metrics": target_metrics,
        "group_by": group_by[:2],
        "time": time_field,
        "requested_subject": requested_subject or "dashboard",
    }
