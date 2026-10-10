from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from engine.core.models.abstract_spec import AbstractSpec, ColumnRef, DashboardPage, DashboardSpec, DataBinding, DataLineageSpec, Measure, MeasureRef, SemanticModel, TableRef, VisualSpec
from engine.core.models.report_pipeline import GeneratedReport, NormalizedDashboard, ReportIndex, ReportMode
from engine.extraction.dashboard_extractor import DashboardExtractor
from engine.generation.report_orchestrator import ReportOrchestrator
from engine.layout.layout_models import GridConfig
from engine.phases.phase3_spec.spec_validator import fix_chart_spec
from engine.phases.phase4_transform.rdl_dataset_mapper import RDLField, RDLDataset
from engine.phases.phase5_rdl.rdl_builder import RDLBuilder
from engine.phases.phase5_rdl.rdl_layout_builder import RDLLayoutBuilder
from engine.phases.phase5_rdl.rdl_visual_mapper import RDLVisualMapper

_log = logging.getLogger(__name__)


def _normalize_aggregation(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"avg", "average", "mean"}:
        return "avg"
    if text in {"count", "countdistinct"}:
        return "count"
    if text in {"min", "minimum"}:
        return "min"
    if text in {"max", "maximum"}:
        return "max"
    return "sum"


def _infer_rdl_type(column_type: str | None) -> str:
    value = str(column_type or "").strip().lower()
    if any(token in value for token in ["int", "integer", "number", "numeric", "float", "double", "decimal", "real"]):
        return "Decimal"
    if any(token in value for token in ["date", "time", "datetime", "timestamp"]):
        return "DateTime"
    return "String"


def _safe_identifier(value: Any, fallback: str) -> str:
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in str(value or "")).strip("_")
    if not cleaned:
        cleaned = fallback
    if not cleaned:
        return fallback
    if not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"_{cleaned}"
    return cleaned[:120]


def _build_structured_spec(
    dashboard_model: dict[str, object],
    prompt_info: dict[str, object] | None = None,
    schema_info: dict[str, object] | None = None,
) -> AbstractSpec:
    prompt_info = prompt_info if isinstance(prompt_info, dict) else {}
    schema_info = schema_info if isinstance(schema_info, dict) else {}

    elements = dashboard_model.get("elements", []) if isinstance(dashboard_model.get("elements"), list) else []
    first_element = next((element for element in elements if isinstance(element, dict)), {})
    metadata = dashboard_model.get("metadata", {}) if isinstance(dashboard_model.get("metadata"), dict) else {}
    table_name = str(
        schema_info.get("primary_table")
        or metadata.get("primary_table")
        or ((schema_info.get("tables") or [{}])[0].get("name") if isinstance(schema_info.get("tables"), list) else "")
        or "DataSource"
    ).strip() or "DataSource"

    schema_tables = schema_info.get("tables", []) if isinstance(schema_info.get("tables"), list) else []
    schema_columns = []
    if schema_tables:
        first_table = schema_tables[0] if isinstance(schema_tables[0], dict) else {}
        schema_columns = first_table.get("columns", []) if isinstance(first_table.get("columns"), list) else []

    element_data = first_element.get("data", {}) if isinstance(first_element.get("data"), dict) else {}
    x_field = str(element_data.get("x") or next((item for item in (metadata.get("dimensions", []) or []) if isinstance(item, str)), "fuel_type")).strip()
    y_field = str(element_data.get("y") or next((item for item in (metadata.get("measures", []) or []) if isinstance(item, str)), "price_pence")).strip()
    aggregation = _normalize_aggregation(element_data.get("aggregation") or prompt_info.get("aggregation") or prompt_info.get("metric_aggregation"))
    if any(token in str(prompt_info.get("requested_subject") or "").lower() for token in ["average", "avg", "mean"]):
        aggregation = "avg"

    visual_title = str(first_element.get("title") or dashboard_model.get("title") or "Generated Visual").strip() or "Generated Visual"
    # Ensure axes include the real SQL table name for each bound column
    visual = VisualSpec(
        id=_safe_identifier(first_element.get("id") or "visual_1", "visual_1"),
        source_worksheet="GeneratedWorksheet",
        type="chart",
        rdl_type="chart",
        title=visual_title,
        position=first_element.get("position", {}) if isinstance(first_element.get("position"), dict) else {},
        data_binding=DataBinding(
            axes={
                "x": ColumnRef(table=table_name, column=x_field),
                # Use ColumnRef for y as well so the binding contains the source table
                "y": ColumnRef(table=table_name, column=y_field),
            },
            measures=[MeasureRef(name=y_field)],
            group_by=[x_field],
            aggregation=aggregation,
        ),
    )

    page = DashboardPage(id="page_1", name=str(dashboard_model.get("title") or "Generated Dashboard"), visuals=[visual])
    dashboard_spec = DashboardSpec(pages=[page], global_filters=[])

    semantic_model = SemanticModel(
        entities=[SimpleNamespace(name=table_name, columns=schema_columns)],
        measures=[Measure(name=y_field, expression=f"{aggregation.upper()}({y_field})")],
        dimensions=[x_field],
        fact_table=table_name,
        grain="row",
    )

    lineage_columns = []
    for column in schema_columns:
        if not isinstance(column, dict):
            continue
        column_name = str(column.get("name") or "").strip()
        if not column_name:
            continue
        lineage_columns.append(
            {
                "name": column_name,
                "dtype": str(column.get("type") or "string"),
                "is_numeric": str(column.get("type") or "").strip().lower() in {"numeric", "number", "float", "double", "decimal", "int", "integer"},
                "is_date": str(column.get("type") or "").strip().lower() in {"date", "datetime", "timestamp", "time"},
            }
        )

    data_lineage = DataLineageSpec(
        tables=[TableRef(id="t1", name=table_name, source_name=table_name, columns=[])],
        joins=[],
        columns_used=[],
        measures=[],
        filters_applied=[],
        transformations=[],
        visual_column_map={},
    )
    spec = AbstractSpec(
        dashboard_spec=dashboard_spec,
        semantic_model=semantic_model,
        data_lineage=data_lineage,
    )

    fields: list[RDLField] = []
    seen: set[str] = set()

    def add_field(name: str, column_type: str | None = None) -> None:
        cleaned_name = _safe_identifier(name, "Field")
        lowered = cleaned_name.lower()
        if not cleaned_name or lowered in seen:
            return
        seen.add(lowered)
        fields.append(RDLField(name=cleaned_name, data_field=str(name), rdl_type=_infer_rdl_type(column_type)))

    for column in schema_columns:
        if not isinstance(column, dict):
            continue
        add_field(str(column.get("name") or ""), str(column.get("type") or ""))

    add_field(x_field, "string")
    add_field(y_field, "decimal")

    projection_fields = [str(field.data_field) for field in fields if str(field.data_field or "").strip()]
    if not projection_fields:
        projection_fields = [x_field, y_field]

    dataset = RDLDataset(
        name=table_name,
        query=f"SELECT {', '.join(projection_fields)} FROM {table_name}",
        connection_ref="DataSource1",
        fields=fields,
    )
    spec.rdl_datasets = [dataset]
    return spec


def generate_rdl(
    dashboard_model: dict[str, object],
    prompt_info: dict[str, object] | None = None,
    schema_info: dict[str, object] | None = None,
) -> str:
    """Convert the dashboard model into a valid SSRS 2016 RDL XML document."""
    spec = _build_structured_spec(dashboard_model, prompt_info=prompt_info, schema_info=schema_info)
    layout_builder = RDLLayoutBuilder()
    page = spec.dashboard_spec.pages[0]
    layouts = {page.name: layout_builder.compute_layout(page, page.visuals)}
    rdl_pages = layout_builder.compute_pagination(spec.dashboard_spec.pages)
    builder = RDLBuilder(visual_mapper=RDLVisualMapper(use_llm=False, semantic_model=spec.semantic_model))
    xml_text, _ = builder.build_xml(spec, layouts, rdl_pages)
    return xml_text


async def generate_multi_rdl(
    spec: AbstractSpec,
    mode: ReportMode = ReportMode.SINGLE,
    dashboards: list[NormalizedDashboard] | None = None,
    use_llm_planner: bool = False,
    llm_client: Any = None,
    grid_config: GridConfig | None = None,
) -> tuple[list[GeneratedReport], ReportIndex]:
    """Multi-dashboard RDL generation with single or multiple report output.

    Parameters
    ----------
    spec:
        Fully-built AbstractSpec from the existing pipeline (phases 0-5).
    mode:
        ReportMode.SINGLE  → all dashboards merged into one multi-page RDL.
        ReportMode.MULTIPLE → each dashboard becomes an independent RDL file
                              (generated in parallel).
    dashboards:
        Pre-extracted NormalizedDashboard list. When None the function
        auto-extracts from spec.dashboard_spec.pages.
    use_llm_planner:
        Enable the optional LLM layout planner (requires llm_client).
    llm_client:
        Any async client that exposes `await client.complete(prompt) -> str`.
    grid_config:
        Override the 12-column grid configuration (margins, gutters, heights).

    Returns
    -------
    (reports, index)
        reports : list of GeneratedReport with rdl_content + filename per report
        index   : ReportIndex with JSON-serialisable metadata for navigation
    """
    if dashboards is None:
        extractor = DashboardExtractor()
        dashboards = extractor.extract_from_spec(spec)

    spec, fix_reports = fix_chart_spec(spec, spec.semantic_model)
    for report in fix_reports:
        if report.corrected:
            _log.warning(
                "spec_validator corrected visual '%s': %s",
                report.visual_id,
                "; ".join(report.changes),
            )

    orchestrator = ReportOrchestrator(
        use_llm_planner=use_llm_planner,
        llm_client=llm_client,
        grid_config=grid_config,
    )
    return await orchestrator.generate(dashboards, mode, spec)
