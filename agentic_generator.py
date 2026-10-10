from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re

from engine.core.ir.models import (
    IRAggregation,
    IRDataset,
    IREncoding,
    IRField,
    IRLayout,
    IRLineage,
    IRMetadata,
    IRPage,
    IRPageVisual,
    IRPosition,
    IRVisual,
    IntermediateRepresentation,
)
from engine.core.ir.validator import IRValidator


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "")).strip("_").lower()
    return cleaned or "field"


@dataclass
class SchemaSummary:
    entities: list[str]
    measures: list[str]
    dimensions: list[str]
    temporal: list[str]


class SchemaAnalyzer:
    """Analyze semantic graph and categorize available fields."""

    def analyze(self, semantic_graph: dict[str, Any]) -> SchemaSummary:
        entities = [str(entity) for entity in semantic_graph.get("entities", []) if str(entity).strip()]
        fields = semantic_graph.get("fields", []) if isinstance(semantic_graph.get("fields"), list) else []

        measures: list[str] = []
        dimensions: list[str] = []
        temporal: list[str] = []

        for field in fields:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "").strip()
            role = str(field.get("type") or "").strip().lower()
            if not name:
                continue
            if role == "measure":
                measures.append(name)
            elif role == "temporal":
                temporal.append(name)
                dimensions.append(name)
            else:
                dimensions.append(name)

        return SchemaSummary(
            entities=list(dict.fromkeys(entities)),
            measures=list(dict.fromkeys(measures)),
            dimensions=list(dict.fromkeys(dimensions)),
            temporal=list(dict.fromkeys(temporal)),
        )


class IntentInterpreter:
    """Interpret user query into structured intent and constraints."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def interpret(self, user_query: str, schema: SchemaSummary) -> dict[str, Any]:
        text = str(user_query or "").strip()
        lower = text.lower()

        visual_preferences: list[str] = []
        if "bar" in lower:
            visual_preferences.append("bar_chart")
        if "line" in lower or "trend" in lower or "over time" in lower:
            visual_preferences.append("line_chart")
        if "pie" in lower:
            visual_preferences.append("pie_chart")
        if "kpi" in lower:
            visual_preferences.append("kpi")

        max_visuals: int | None = None
        match_max = re.search(r"(max|maximum)\s*(\d+)\s*(visual|chart)s?", lower)
        if match_max:
            max_visuals = int(match_max.group(2))

        required_visuals: list[dict[str, Any]] = []
        count_matches = re.finditer(r"(\d+)\s*(bar|line|pie|kpi|table|scatter|area)\s*(chart|charts|visual|visuals)?", lower)
        for match in count_matches:
            count = int(match.group(1))
            chart_type = f"{match.group(2)}_chart" if match.group(2) not in {"kpi", "table"} else match.group(2)
            required_visuals.append({"type": chart_type, "count": count})

        if "one kpi" in lower or "only one kpi" in lower:
            required_visuals.append({"type": "kpi", "count": 1})

        explicit_goal = "overview"
        if any(token in lower for token in ["variation", "trend", "evolution", "over time"]):
            explicit_goal = "trend"
        elif any(token in lower for token in ["compare", "comparison", "by "]):
            explicit_goal = "comparison"
        elif any(token in lower for token in ["distribution", "share", "breakdown"]):
            explicit_goal = "distribution"

        is_vague = not text or len(text.split()) <= 3 or text.lower() in {"create a dashboard", "creer un dashboard", "dashboard"}

        constraints: dict[str, Any] = {
            "max_visuals": max_visuals,
            "types": visual_preferences or None,
            "required_visuals": required_visuals or None,
            "strict": bool(required_visuals or max_visuals is not None or any(token in lower for token in ["exactly", "strict", "only"])),
            "exact_match": bool(any(token in lower for token in ["exact match", "exactly", "strictly", "must have", "must include"])),
            "only_one_kpi": bool("only one kpi" in lower or "one kpi" in lower),
        }

        return {
            "goal": explicit_goal,
            "visual_preferences": visual_preferences,
            "constraints": constraints,
            "is_vague": is_vague,
            "requested_metrics": [metric for metric in schema.measures if metric.lower() in lower],
            "requested_dimensions": [dimension for dimension in schema.dimensions if dimension.lower() in lower],
        }


class InsightPlanner:
    """Plan insight tasks from schema and interpreted intent."""

    def plan(self, schema: SchemaSummary, intent: dict[str, Any]) -> list[dict[str, Any]]:
        requested_metrics = intent.get("requested_metrics", []) if isinstance(intent.get("requested_metrics"), list) else []
        requested_dimensions = intent.get("requested_dimensions", []) if isinstance(intent.get("requested_dimensions"), list) else []

        metric = requested_metrics[0] if requested_metrics else (schema.measures[0] if schema.measures else "")
        temporal = schema.temporal[0] if schema.temporal else ""
        dimension = requested_dimensions[0] if requested_dimensions else next((item for item in schema.dimensions if item != temporal), "")

        plan: list[dict[str, Any]] = []
        goal = str(intent.get("goal") or "overview")

        if not metric:
            return plan

        if goal in {"trend", "overview"} and temporal:
            plan.append({"analysis": "trend", "x": temporal, "y": metric})
        if goal in {"comparison", "overview", "distribution"} and dimension:
            plan.append({"analysis": "comparison", "x": dimension, "y": metric})
        plan.append({"analysis": "kpi", "metric": metric})

        # Zero-shot default composition.
        if intent.get("is_vague"):
            if temporal and not any(item.get("analysis") == "trend" for item in plan):
                plan.insert(0, {"analysis": "trend", "x": temporal, "y": metric})
            if dimension and not any(item.get("analysis") == "comparison" for item in plan):
                plan.append({"analysis": "comparison", "x": dimension, "y": metric})

        dedup: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in plan:
            key = (str(item.get("analysis")), str(item.get("x")), str(item.get("y") or item.get("metric")))
            if key in seen:
                continue
            seen.add(key)
            dedup.append(item)
        return dedup


class VisualizationPlanner:
    """Map insights to visuals and generate multiple candidates when needed."""

    def build_candidates(self, insight_plan: list[dict[str, Any]], intent: dict[str, Any]) -> list[list[dict[str, Any]]]:
        preferred = intent.get("visual_preferences", []) if isinstance(intent.get("visual_preferences"), list) else []
        constraints = intent.get("constraints", {}) if isinstance(intent.get("constraints"), dict) else {}
        allowed_types = set(constraints.get("types", []) or [])
        max_visuals = int(constraints.get("max_visuals") or 0) if constraints.get("max_visuals") else None
        required_visuals = constraints.get("required_visuals", []) if isinstance(constraints.get("required_visuals"), list) else []
        strict_mode = bool(constraints.get("strict"))
        exact_match = bool(constraints.get("exact_match"))
        only_one_kpi = bool(constraints.get("only_one_kpi"))

        base: list[dict[str, Any]] = []
        alt: list[dict[str, Any]] = []

        for insight in insight_plan:
            analysis = str(insight.get("analysis") or "")
            if analysis == "trend":
                base.append({"type": "line_chart", "x": insight.get("x"), "y": insight.get("y"), "aggregation": "sum"})
                alt.append({"type": "bar_chart", "x": insight.get("x"), "y": insight.get("y"), "aggregation": "sum"})
            elif analysis == "comparison":
                base.append({"type": "bar_chart", "x": insight.get("x"), "y": insight.get("y"), "aggregation": "sum"})
                alt.append({"type": "line_chart", "x": insight.get("x"), "y": insight.get("y"), "aggregation": "sum"})
            elif analysis == "kpi":
                base.append({"type": "kpi", "metric": insight.get("metric")})
                alt.append({"type": "kpi", "metric": insight.get("metric")})

        if preferred:
            for idx, visual in enumerate(base):
                if visual.get("type") != "kpi" and preferred:
                    base[idx]["type"] = preferred[0]

        candidates = [base, alt]
        normalized: list[list[dict[str, Any]]] = []
        for candidate in candidates:
            filtered = [visual for visual in candidate if self._is_allowed(visual, allowed_types)]
            if max_visuals is not None:
                filtered = filtered[:max_visuals]
            if only_one_kpi and self._count_visual_type(filtered, "kpi") > 1:
                continue
            if exact_match and required_visuals and not self._meets_required_counts(filtered, required_visuals):
                continue
            if filtered:
                normalized.append(filtered)

        if required_visuals:
            strict_candidate = self._build_required_candidate(required_visuals, insight_plan, allowed_types)
            if strict_candidate:
                if max_visuals is not None:
                    strict_candidate = strict_candidate[:max_visuals]
                if only_one_kpi and self._count_visual_type(strict_candidate, "kpi") > 1:
                    strict_candidate = [visual for visual in strict_candidate if visual.get("type") != "kpi"]
                if not exact_match or self._meets_required_counts(strict_candidate, required_visuals):
                    normalized.insert(0, strict_candidate)

        # Minimal strict candidate from first non-kpi visual.
        compact = [visual for visual in base if visual.get("type") != "kpi"]
        if compact:
            compact = compact[:1]
            if compact not in normalized:
                normalized.append(compact)

        if strict_mode and normalized:
            normalized = [candidate for candidate in normalized if candidate]

        if exact_match and required_visuals:
            exact_candidates = [candidate for candidate in normalized if self._meets_required_counts(candidate, required_visuals)]
            if exact_candidates:
                normalized = exact_candidates

        return normalized or [base[:1]]

    def _count_visual_type(self, visuals: list[dict[str, Any]], visual_type: str) -> int:
        return sum(1 for visual in visuals if str(visual.get("type") or "") == visual_type)

    def _meets_required_counts(self, visuals: list[dict[str, Any]], required_visuals: list[dict[str, Any]]) -> bool:
        counts: dict[str, int] = {}
        for visual in visuals:
            visual_type = str(visual.get("type") or "")
            counts[visual_type] = counts.get(visual_type, 0) + 1

        for requirement in required_visuals:
            if not isinstance(requirement, dict):
                continue
            chart_type = str(requirement.get("type") or "").strip()
            required_count = int(requirement.get("count") or 1)
            if counts.get(chart_type, 0) < required_count:
                return False
        return True

    def _build_required_candidate(
        self,
        required_visuals: list[dict[str, Any]],
        insight_plan: list[dict[str, Any]],
        allowed_types: set[str],
    ) -> list[dict[str, Any]]:
        candidate: list[dict[str, Any]] = []
        for requirement in required_visuals:
            if not isinstance(requirement, dict):
                continue
            chart_type = str(requirement.get("type") or "").strip()
            count = int(requirement.get("count") or 1)
            for _ in range(max(1, count)):
                for insight in insight_plan:
                    visual = self._visual_from_requirement(chart_type, insight)
                    if visual and self._is_allowed(visual, allowed_types):
                        candidate.append(visual)
                        break
        return candidate

    def _visual_from_requirement(self, chart_type: str, insight: dict[str, Any]) -> dict[str, Any] | None:
        analysis = str(insight.get("analysis") or "")
        if chart_type == "kpi":
            metric = insight.get("metric") or insight.get("y")
            return {"type": "kpi", "metric": metric, "title": f"KPI {metric}"} if metric else None
        if chart_type == "pie_chart" and analysis in {"comparison", "distribution"}:
            return {"type": "pie_chart", "x": insight.get("x"), "y": insight.get("y"), "aggregation": "sum", "title": "Distribution"}
        if chart_type == "line_chart" and analysis == "trend":
            return {"type": "line_chart", "x": insight.get("x"), "y": insight.get("y"), "aggregation": "sum", "title": "Trend"}
        if chart_type == "bar_chart" and analysis in {"comparison", "trend", "kpi"}:
            x_value = insight.get("x") or insight.get("metric")
            y_value = insight.get("y") or insight.get("metric")
            return {"type": "bar_chart", "x": x_value, "y": y_value, "aggregation": "sum", "title": "Comparison"}
        if chart_type == "table":
            return {"type": "table", "columns": [insight.get("x"), insight.get("y")], "title": "Details"}
        if chart_type == "scatter_chart":
            return {"type": "scatter_chart", "x": insight.get("x"), "y": insight.get("y"), "title": "Scatter"}
        if chart_type == "area_chart":
            return {"type": "area_chart", "x": insight.get("x"), "y": insight.get("y"), "title": "Area"}
        return None

    def _is_allowed(self, visual: dict[str, Any], allowed_types: set[str]) -> bool:
        if not allowed_types:
            return True
        return str(visual.get("type") or "") in allowed_types


class Scorer:
    """Score candidate dashboards by correctness, diversity and relevance."""

    def score(self, candidate: list[dict[str, Any]], schema: SchemaSummary, intent: dict[str, Any]) -> dict[str, float]:
        correctness = 1.0
        diversity = 0.0
        relevance = 0.0
        coverage = 0.0
        exactness = 0.0
        constraint_penalty = 0.0

        types = [str(item.get("type") or "") for item in candidate]
        diversity = min(1.0, len(set(types)) / 3.0)

        valid_fields = set(schema.measures + schema.dimensions)
        valid_count = 0
        field_refs = 0
        for visual in candidate:
            for key in ["x", "y", "metric"]:
                if key in visual and visual.get(key):
                    field_refs += 1
                    if str(visual.get(key)) in valid_fields:
                        valid_count += 1
        if field_refs:
            correctness = valid_count / field_refs

        goal = str(intent.get("goal") or "overview")
        has_trend = any(item.get("type") == "line_chart" for item in candidate)
        has_comparison = any(item.get("type") == "bar_chart" for item in candidate)
        has_kpi = any(item.get("type") == "kpi" for item in candidate)

        if goal == "trend" and has_trend:
            relevance = 1.0
        elif goal == "comparison" and has_comparison:
            relevance = 1.0
        elif goal == "overview" and (has_kpi or has_trend or has_comparison):
            relevance = 0.9
        else:
            relevance = 0.6

        coverage, exactness, constraint_penalty = self._score_constraints(candidate, intent)

        global_score = round(
            max(0.0, min(1.0, (0.35 * correctness) + (0.2 * relevance) + (0.1 * diversity) + (0.2 * coverage) + (0.15 * exactness) - constraint_penalty)),
            4,
        )
        return {
            "correctness": round(correctness, 4),
            "relevance": round(relevance, 4),
            "diversity": round(diversity, 4),
            "coverage": round(coverage, 4),
            "exactness": round(exactness, 4),
            "constraint_penalty": round(constraint_penalty, 4),
            "score": global_score,
        }

    def _score_constraints(self, candidate: list[dict[str, Any]], intent: dict[str, Any]) -> tuple[float, float, float]:
        constraints = intent.get("constraints", {}) if isinstance(intent.get("constraints"), dict) else {}
        required_visuals = constraints.get("required_visuals", []) if isinstance(constraints.get("required_visuals"), list) else []
        max_visuals = constraints.get("max_visuals")
        exact_match = bool(constraints.get("exact_match"))
        only_one_kpi = bool(constraints.get("only_one_kpi"))

        coverage = 1.0
        exactness = 0.0
        penalty = 0.0

        if required_visuals:
            total_required = sum(int(item.get("count") or 1) for item in required_visuals if isinstance(item, dict)) or 1
            counts: dict[str, int] = {}
            for visual in candidate:
                visual_type = str(visual.get("type") or "")
                counts[visual_type] = counts.get(visual_type, 0) + 1

            matched = 0
            for requirement in required_visuals:
                if not isinstance(requirement, dict):
                    continue
                chart_type = str(requirement.get("type") or "").strip()
                required_count = int(requirement.get("count") or 1)
                matched += min(counts.get(chart_type, 0), required_count)

            coverage = matched / total_required
            exactness = 1.0 if matched == total_required and len(candidate) == total_required else coverage * 0.8

        if max_visuals is not None and len(candidate) > int(max_visuals):
            penalty += min(0.5, 0.1 * (len(candidate) - int(max_visuals)))

        if only_one_kpi and self._count_type(candidate, "kpi") > 1:
            penalty += 0.35

        if exact_match:
            if required_visuals and coverage == 1.0 and len(candidate) == sum(int(item.get("count") or 1) for item in required_visuals if isinstance(item, dict)):
                exactness = 1.0
            else:
                penalty += 0.25

        return coverage, exactness, penalty

    def _count_type(self, candidate: list[dict[str, Any]], visual_type: str) -> int:
        return sum(1 for visual in candidate if str(visual.get("type") or "") == visual_type)


class IRBuilder:
    """Build canonical IR from selected visuals and semantic graph."""

    def build(self, semantic_graph: dict[str, Any], visuals: list[dict[str, Any]], *, confidence: float) -> IntermediateRepresentation:
        entity_name = str((semantic_graph.get("entities") or ["dataset"])[0])
        dataset_id = _slug(entity_name)

        fields: list[IRField] = []
        for field in semantic_graph.get("fields", []):
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or "").strip()
            role = str(field.get("type") or "dimension").strip().lower()
            if not field_name:
                continue
            mapped_role = "dimension"
            if role == "measure":
                mapped_role = "measure"
            elif role == "temporal":
                mapped_role = "temporal"
            fields.append(
                IRField(
                    id=f"{dataset_id}.{_slug(field_name)}",
                    name=field_name,
                    type=mapped_role,
                    data_type="number" if mapped_role == "measure" else "date" if mapped_role == "temporal" else "string",
                    source_table=entity_name,
                )
            )

        dataset = IRDataset(id=dataset_id, name=entity_name, fields=fields)
        field_index = {field.name: field.id for field in fields}

        ir_visuals: list[IRVisual] = []
        page_visuals: list[IRPageVisual] = []
        for idx, visual in enumerate(visuals, start=1):
            visual_id = f"visual_{idx}"
            vtype = str(visual.get("type") or "bar_chart")
            x_name = str(visual.get("x") or "")
            y_name = str(visual.get("y") or visual.get("metric") or "")
            x_field = field_index.get(x_name)
            y_field = field_index.get(y_name)

            ir_visual = IRVisual(
                id=visual_id,
                type=vtype if vtype in {"bar_chart", "line_chart", "pie_chart", "kpi", "table", "scatter_chart", "area_chart"} else "unknown",
                dataset_id=dataset_id,
                encoding=IREncoding(x=x_field, y=y_field),
                aggregation=IRAggregation(field=y_field or "", method="sum" if y_field else "none"),
                title=str(visual.get("title") or visual_id),
            )
            ir_visuals.append(ir_visual)
            page_visuals.append(IRPageVisual(visual_id=visual_id, position=IRPosition(x=0 if idx % 2 else 6, y=(idx - 1) * 4, width=6, height=4)))

        ir = IntermediateRepresentation(
            metadata=IRMetadata(source_type="generation", created_by="engine.creation.agentic_generator", confidence=confidence),
            datasets=[dataset],
            visuals=ir_visuals,
            layout=IRLayout(pages=[IRPage(name="Overview", visuals=page_visuals)]),
            lineage=IRLineage(source=entity_name, mappings=[]),
            constraints=[],
        )
        ir.validation = IRValidator().validate(ir)
        return ir


class AgenticDashboardGenerator:
    """End-to-end dashboard generation from semantic graph + user query."""

    def __init__(self, llm_client: Any | None = None):
        self.schema_analyzer = SchemaAnalyzer()
        self.intent_interpreter = IntentInterpreter(llm_client=llm_client)
        self.insight_planner = InsightPlanner()
        self.visualization_planner = VisualizationPlanner()
        self.scorer = Scorer()
        self.ir_builder = IRBuilder()

    def generate(self, semantic_graph: dict[str, Any], user_query: str) -> dict[str, Any]:
        schema = self.schema_analyzer.analyze(semantic_graph)
        intent = self.intent_interpreter.interpret(user_query, schema)
        insight_plan = self.insight_planner.plan(schema, intent)
        candidates = self.visualization_planner.build_candidates(insight_plan, intent)

        scored_candidates: list[dict[str, Any]] = []
        chosen_ir: IntermediateRepresentation | None = None
        chosen_candidate: list[dict[str, Any]] = []
        validation_runs: list[dict[str, Any]] = []
        for candidate in candidates:
            score = self.scorer.score(candidate, schema, intent)
            ir = self.ir_builder.build(
                semantic_graph,
                candidate,
                confidence=float(score.get("score", 0.0)),
            )
            validation_runs.append({"candidate": candidate, "validation": ir.validation.model_dump(mode="json")})
            if ir.validation.issues and any(issue.severity == "error" for issue in ir.validation.issues):
                repaired_candidate = self._repair_candidate(candidate, schema, intent)
                if repaired_candidate != candidate:
                    repaired_score = self.scorer.score(repaired_candidate, schema, intent)
                    repaired_ir = self.ir_builder.build(
                        semantic_graph,
                        repaired_candidate,
                        confidence=float(repaired_score.get("score", 0.0)),
                    )
                    validation_runs.append({"candidate": repaired_candidate, "validation": repaired_ir.validation.model_dump(mode="json")})
                    candidate = repaired_candidate
                    score = repaired_score
                    ir = repaired_ir

            scored_candidates.append({"visuals": candidate, "score": score, "validation": ir.validation.model_dump(mode="json"), "ir": ir.model_dump(mode="json")})

        scored_candidates.sort(key=lambda item: (not item.get("validation", {}).get("issues"), item["score"].get("score", 0.0)), reverse=True)
        selected = next((item for item in scored_candidates if not any(issue.get("severity") == "error" for issue in item.get("validation", {}).get("issues", []))), None)
        if selected is None:
            selected = scored_candidates[0] if scored_candidates else {"visuals": [], "score": {"score": 0.0}, "validation": {"score": 0.0, "issues": []}, "ir": {}}

        ir = IntermediateRepresentation.model_validate(selected.get("ir", {})) if selected.get("ir") else self.ir_builder.build(
            semantic_graph,
            selected.get("visuals", []),
            confidence=float(selected.get("score", {}).get("score", 0.0)),
        )
        if ir.validation.issues and any(issue.severity == "error" for issue in ir.validation.issues):
            repaired_candidate = self._repair_candidate(selected.get("visuals", []), schema, intent)
            if repaired_candidate != selected.get("visuals", []):
                ir = self.ir_builder.build(
                    semantic_graph,
                    repaired_candidate,
                    confidence=float(selected.get("score", {}).get("score", 0.0)),
                )
                selected["visuals"] = repaired_candidate
                selected["validation"] = ir.validation.model_dump(mode="json")
                selected["ir"] = ir.model_dump(mode="json")

        return {
            "intent": intent,
            "insight_plan": insight_plan,
            "candidates": scored_candidates,
            "selected_visuals": selected.get("visuals", []),
            "selected_score": selected.get("score", {}),
            "ir": ir.model_dump(mode="json"),
            "validation": ir.validation.model_dump(mode="json"),
            "validation_runs": validation_runs,
        }

    def _repair_candidate(
        self,
        candidate: list[dict[str, Any]],
        schema: SchemaSummary,
        intent: dict[str, Any],
    ) -> list[dict[str, Any]]:
        repaired = [dict(visual) for visual in candidate if isinstance(visual, dict)]
        if not repaired:
            return repaired

        allowed_fields = set(schema.measures + schema.dimensions)
        if schema.temporal:
            allowed_fields.add(schema.temporal[0])

        for visual in repaired:
            for key in ["x", "y", "metric"]:
                value = visual.get(key)
                if value and value not in allowed_fields:
                    replacement = schema.measures[0] if key in {"y", "metric"} and schema.measures else (schema.dimensions[0] if schema.dimensions else "")
                    if replacement:
                        visual[key] = replacement

        constraints = intent.get("constraints", {}) if isinstance(intent.get("constraints"), dict) else {}
        max_visuals = constraints.get("max_visuals")
        if max_visuals:
            repaired = repaired[: int(max_visuals)]
        return repaired


def build_semantic_graph(extraction: dict[str, Any], semantic_payload: dict[str, Any]) -> dict[str, Any]:
    """Create a semantic graph payload from extraction + semantic agents outputs."""
    source = str(extraction.get("primary_table") or extraction.get("source") or "dataset")
    entity = source.split("/")[-1].split(".")[0] or "dataset"

    columns = extraction.get("columns", []) if isinstance(extraction.get("columns"), list) else []
    semantic_dimensions = set(semantic_payload.get("dimensions", []) if isinstance(semantic_payload.get("dimensions"), list) else [])
    semantic_measures = {
        str(item.get("column") or item.get("name") or "")
        for item in (semantic_payload.get("measures", []) if isinstance(semantic_payload.get("measures"), list) else [])
        if isinstance(item, dict)
    }

    fields: list[dict[str, str]] = []
    for column in columns:
        if not isinstance(column, dict):
            continue
        name = str(column.get("name") or "").strip()
        if not name:
            continue
        if name in semantic_measures or bool(column.get("is_numeric")):
            role = "measure"
        elif bool(column.get("is_date")):
            role = "temporal"
        elif name in semantic_dimensions:
            role = "dimension"
        else:
            role = "dimension"
        fields.append({"name": name, "type": role})

    return {
        "entities": [entity],
        "fields": fields,
        "relationships": [],
    }
