from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine.creation.dashboard_model import build_dashboard_model
from engine.creation.insight_generator import generate_insights
from engine.creation.layout_engine import generate_layout
from engine.creation.prompt_interpreter import interpret_prompt
from engine.creation.rdl_generator import generate_rdl
from engine.creation.schema_analyzer import analyze_schema
from engine.creation.scoring import score_dashboard
from engine.creation.visualization_selector import select_visuals


@dataclass
class DashboardCreationResult:
    schema_info: dict[str, Any]
    prompt_info: dict[str, Any]
    insights: dict[str, Any]
    visuals: list[dict[str, Any]]
    layout: dict[str, Any]
    dashboard_model: dict[str, Any]
    rdl: str
    score: dict[str, Any]


def create_dashboard(input_data: dict[str, Any]) -> DashboardCreationResult:
    """Run the end-to-end dashboard creation module from schema and prompt."""
    schema = input_data.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("input_data['schema'] must be a dictionary")
    user_prompt = str(input_data.get("user_prompt") or "").strip()
    if not user_prompt:
        raise ValueError("input_data['user_prompt'] is required")

    schema_info = analyze_schema(schema)
    prompt_info = interpret_prompt(user_prompt, schema_info)
    insights = generate_insights(schema_info, prompt_info)
    visuals = select_visuals(insights)
    layout = generate_layout(visuals)
    dashboard_model = build_dashboard_model(visuals, layout, prompt_info, schema_info)
    rdl = generate_rdl(dashboard_model, prompt_info=prompt_info, schema_info=schema_info)
    score = score_dashboard(dashboard_model)
    return DashboardCreationResult(schema_info, prompt_info, insights, visuals, layout, dashboard_model, rdl, score)
