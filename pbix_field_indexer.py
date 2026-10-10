from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _norm(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "").replace(" ", "")


def _decode_json(raw: bytes) -> dict[str, Any]:
    for enc in ("utf-16le", "utf-8"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            continue
    return json.loads(raw)


def _agg_name(code: int | None) -> str:
    mapping = {
        0: "sum",
        1: "avg",
        2: "min",
        3: "max",
        4: "count",
        5: "countrows",
        6: "distinctcount",
        7: "none",
    }
    return mapping.get(int(code or 0), "sum")


@dataclass(frozen=True)
class IndexedField:
    entity: str
    property: str
    query_ref: str
    aggregation: str
    is_measure: bool
    lineage_id: str = ""


class PBIXFieldIndexer:
    """Extract and resolve real field metadata from a base PBIX Report/Layout."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], IndexedField] = {}
        self._by_name: dict[str, IndexedField] = {}

    def extract(self, pbix_path: Path) -> "PBIXFieldIndexer":
        with zipfile.ZipFile(pbix_path, "r") as zf:
            if "Report/Layout" not in set(zf.namelist()):
                return self
            layout = _decode_json(zf.read("Report/Layout"))

        for section in layout.get("sections", []) or []:
            if not isinstance(section, dict):
                continue
            for vc in section.get("visualContainers", []) or []:
                if not isinstance(vc, dict):
                    continue
                config_raw = vc.get("config")
                if not isinstance(config_raw, str):
                    continue
                try:
                    config = json.loads(config_raw)
                except Exception:
                    continue
                single = config.get("singleVisual", {}) if isinstance(config.get("singleVisual"), dict) else {}
                query = single.get("prototypeQuery", {}) if isinstance(single.get("prototypeQuery"), dict) else {}
                from_nodes = query.get("From", []) if isinstance(query.get("From"), list) else []
                alias_to_entity: dict[str, str] = {}
                for f in from_nodes:
                    if isinstance(f, dict):
                        alias = str(f.get("Name") or "").strip()
                        entity = str(f.get("Entity") or "").strip()
                        if alias and entity:
                            alias_to_entity[alias] = entity

                for sel in query.get("Select", []) or []:
                    if not isinstance(sel, dict):
                        continue
                    self._consume_select(sel, alias_to_entity)
        return self

    def _store(self, field: IndexedField) -> None:
        key = (_norm(field.entity), _norm(field.property))
        self._by_key[key] = field
        self._by_name[_norm(field.property)] = field

    def _consume_select(self, sel: dict[str, Any], alias_to_entity: dict[str, str]) -> None:
        query_ref = str(sel.get("Name") or "").strip()

        if isinstance(sel.get("Measure"), dict):
            measure = sel["Measure"]
            prop = str(measure.get("Property") or "").strip()
            alias = str(((measure.get("Expression") or {}).get("SourceRef") or {}).get("Source") or "").strip()
            entity = alias_to_entity.get(alias, "")
            if entity and prop:
                self._store(IndexedField(entity=entity, property=prop, query_ref=query_ref or f"{entity}.{prop}", aggregation="none", is_measure=True))
            return

        if isinstance(sel.get("Column"), dict):
            column = sel["Column"]
            prop = str(column.get("Property") or "").strip()
            alias = str(((column.get("Expression") or {}).get("SourceRef") or {}).get("Source") or "").strip()
            entity = alias_to_entity.get(alias, "")
            if entity and prop:
                self._store(IndexedField(entity=entity, property=prop, query_ref=query_ref or f"{entity}.{prop}", aggregation="none", is_measure=False))
            return

        if isinstance(sel.get("Aggregation"), dict):
            agg = sel["Aggregation"]
            col = (agg.get("Expression") or {}).get("Column") or {}
            prop = str(col.get("Property") or "").strip()
            alias = str(((col.get("Expression") or {}).get("SourceRef") or {}).get("Source") or "").strip()
            entity = alias_to_entity.get(alias, "")
            if entity and prop:
                self._store(
                    IndexedField(
                        entity=entity,
                        property=prop,
                        query_ref=query_ref or f"{entity}.{prop}",
                        aggregation=_agg_name(agg.get("Function")),
                        is_measure=False,
                    )
                )

    def resolve(self, field_name: str, table_hint: str = "", role_hint: str = "", aggregation: str = "") -> IndexedField | None:
        hint = _norm(table_hint)
        name = _norm(field_name)
        if hint:
            by_key = self._by_key.get((hint, name))
            if by_key:
                return by_key

        by_name = self._by_name.get(name)
        if not by_name:
            return None

        if role_hint == "measure" and not by_name.is_measure and by_name.aggregation == "none":
            if aggregation:
                return IndexedField(
                    entity=by_name.entity,
                    property=by_name.property,
                    query_ref=by_name.query_ref,
                    aggregation=aggregation.lower(),
                    is_measure=False,
                    lineage_id=by_name.lineage_id,
                )
        return by_name

    def as_dict(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for field in sorted(self._by_name.values(), key=lambda f: (f.entity.lower(), f.property.lower())):
            out[field.property.lower()] = {
                "entity": field.entity,
                "property": field.property,
                "queryRef": field.query_ref,
                "aggregation": field.aggregation,
                "lineage_id": field.lineage_id,
                "is_measure": field.is_measure,
            }
        return out
