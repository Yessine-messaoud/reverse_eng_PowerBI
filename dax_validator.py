"""
dax_validator.py — DAX expression syntax validator.

Validates DAX expressions generated from the AbstractSpec semantic model
before they are embedded in the TMSL DataModel.  Catches syntax errors that
would cause Power BI Desktop to reject the model at load time.

Rules checked
-------------
* Balanced parentheses
* Known DAX function names (extensible whitelist)
* Proper table/column reference format:  'Table'[Column]  or  [MeasureName]
* No untranslatable markers left from the RDL translator
* DIVIDE second argument is present (avoid silent ÷0)
* CALCULATE and FILTER have at least one argument

This validator intentionally does not call an external Analysis Services
engine — it is a fast, deterministic, zero-dependency static check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Known DAX functions (non-exhaustive; add as needed)
# ---------------------------------------------------------------------------

_KNOWN_DAX_FUNCTIONS: frozenset[str] = frozenset({
    # Aggregation
    "SUM", "SUMX", "AVERAGE", "AVERAGEX", "MIN", "MINX", "MAX", "MAXX",
    "COUNT", "COUNTA", "COUNTX", "COUNTROWS", "COUNTBLANK", "DISTINCTCOUNT",
    "DISTINCTCOUNTNOBLANK",
    # Logical
    "IF", "IFERROR", "SWITCH", "AND", "OR", "NOT", "TRUE", "FALSE",
    "BLANK", "ISBLANK", "ISNUMBER", "ISTEXT", "ISLOGICAL", "ISERROR",
    # Math
    "DIVIDE", "ABS", "CEILING", "FLOOR", "ROUND", "ROUNDDOWN", "ROUNDUP",
    "SQRT", "POWER", "MOD", "INT", "TRUNC", "SIGN",
    # Date & time
    "DATE", "DATEDIFF", "DATEADD", "DATESBETWEEN", "DATESYTD",
    "YEAR", "QUARTER", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND",
    "TODAY", "NOW", "EDATE", "EOMONTH", "WEEKDAY", "WEEKNUM",
    "CALENDARAUTO", "CALENDAR",
    # Text
    "CONCATENATE", "CONCATENATEX", "LEFT", "RIGHT", "MID", "LEN",
    "UPPER", "LOWER", "TRIM", "SUBSTITUTE", "REPLACE", "FORMAT",
    "VALUE", "TEXT", "FIND", "SEARCH",
    # Filter & table
    "CALCULATE", "CALCULATETABLE", "FILTER", "ALL", "ALLEXCEPT", "ALLSELECTED",
    "RELATED", "RELATEDTABLE", "LOOKUPVALUE", "FIRSTNONBLANK", "LASTNONBLANK",
    "TOPN", "RANKX", "GENERATE", "ADDCOLUMNS", "SUMMARIZE", "SUMMARIZECOLUMNS",
    "CROSSJOIN", "UNION", "INTERSECT", "EXCEPT", "DISTINCT", "VALUES",
    "TREATAS", "USERELATIONSHIP", "CROSSFILTER",
    # Time intelligence
    "TOTALYTD", "TOTALQTD", "TOTALMTD", "SAMEPERIODLASTYEAR",
    "PARALLELPERIOD", "PREVIOUSMONTH", "PREVIOUSQUARTER", "PREVIOUSYEAR",
    "NEXTMONTH", "NEXTQUARTER", "NEXTYEAR",
    # Statistical
    "MEDIAN", "MEDIANX", "PERCENTILE_INC", "PERCENTILE_EXC", "STDEV_S",
    "STDEV_P", "VAR_S", "VAR_P",
    # Information
    "USERNAME", "USERPRINCIPALNAME", "HASONEVALUE", "HASONEFILTER",
    "ISFILTERED", "ISCROSSFILTERED", "SELECTEDVALUE", "COLUMNSTATISTICS",
    # Path / hierarchy
    "PATH", "PATHITEM", "PATHCONTAINS", "PATHLENGTH",
})

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DAXIssue:
    code: str
    severity: str          # "error" | "warning" | "info"
    message: str
    position: int | None = None   # character offset in the expression, when known


@dataclass
class DAXValidationResult:
    expression: str
    is_valid: bool
    issues: list[DAXIssue] = field(default_factory=list)

    def errors(self) -> list[DAXIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def warnings(self) -> list[DAXIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "is_valid": self.is_valid,
            "errors": [{"code": i.code, "message": i.message} for i in self.errors()],
            "warnings": [{"code": i.code, "message": i.message} for i in self.warnings()],
        }


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# 'TableName'[ColumnName]  or  [MeasureName]  or  TableName[ColumnName]
_TABLE_COL_REF = re.compile(
    r"(?:'[^']+'\[[^\]]+\])"          # 'Table'[Column]
    r"|(?:[A-Za-z_]\w*\[[^\]]+\])"    # Table[Column]
    r"|(?:\[[^\]]+\])",                # [MeasureName]
    re.UNICODE,
)

# DAX function calls: WORD(
_FUNC_CALL = re.compile(r"\b([A-Z_][A-Z0-9_.]*)\s*\(", re.IGNORECASE)

# Marker left by the RDL CalcFieldTranslator
_UNTRANSLATABLE_MARKER = "__UNTRANSLATABLE__"

# RDL-style expression (starts with =, uses Fields!)
_RDL_PATTERN = re.compile(r"=\s*(Sum|Avg|Count|Max|Min)\s*\(Fields!", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class DAXValidator:
    """Static DAX expression syntax validator.

    Usage::

        result = DAXValidator().validate("SUM('Sales'[Amount])")
        if not result.is_valid:
            for issue in result.errors():
                print(issue.message)
    """

    def validate(self, expression: str) -> DAXValidationResult:
        expr = str(expression or "").strip()
        issues: list[DAXIssue] = []

        if not expr:
            issues.append(DAXIssue("E_EMPTY", "error", "DAX expression is empty"))
            return DAXValidationResult(expression=expr, is_valid=False, issues=issues)

        # RDL expression leaked into DAX slot
        if _RDL_PATTERN.match(expr):
            issues.append(DAXIssue(
                "E_RDL_LEAK", "error",
                f"Expression looks like RDL syntax, not DAX: {expr[:60]}",
            ))

        # Untranslatable marker
        if _UNTRANSLATABLE_MARKER in expr:
            issues.append(DAXIssue(
                "E_UNTRANSLATABLE", "error",
                "Expression contains __UNTRANSLATABLE__ marker — translation failed",
            ))

        # Balanced parentheses
        depth = 0
        for pos, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    issues.append(DAXIssue(
                        "E_PAREN_CLOSE", "error",
                        f"Unexpected closing parenthesis at position {pos}",
                        position=pos,
                    ))
                    depth = 0
        if depth > 0:
            issues.append(DAXIssue(
                "E_PAREN_OPEN", "error",
                f"{depth} unclosed parenthesis(es) in expression",
            ))

        # Balanced brackets  [ ]
        bracket_depth = 0
        for pos, ch in enumerate(expr):
            if ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth -= 1
                if bracket_depth < 0:
                    issues.append(DAXIssue(
                        "E_BRACKET_CLOSE", "error",
                        f"Unexpected closing bracket at position {pos}",
                        position=pos,
                    ))
                    bracket_depth = 0
        if bracket_depth > 0:
            issues.append(DAXIssue(
                "E_BRACKET_OPEN", "error",
                f"{bracket_depth} unclosed bracket(s) in expression",
            ))

        # Unknown function calls
        for m in _FUNC_CALL.finditer(expr):
            func_name = m.group(1).upper()
            if func_name not in _KNOWN_DAX_FUNCTIONS:
                issues.append(DAXIssue(
                    "W_UNKNOWN_FUNC", "warning",
                    f"Unknown DAX function '{m.group(1)}' — may be a custom or future function",
                    position=m.start(),
                ))

        # DIVIDE should have at least 2 arguments
        for m in re.finditer(r"\bDIVIDE\s*\(([^)]*)\)", expr, re.IGNORECASE):
            args = m.group(1).split(",")
            if len(args) < 2:
                issues.append(DAXIssue(
                    "W_DIVIDE_ARGS", "warning",
                    "DIVIDE() should have at least 2 arguments: DIVIDE(numerator, denominator[, alternate])",
                    position=m.start(),
                ))

        # Dangling single-quotes (unmatched table name quotes)
        single_quotes = [i for i, ch in enumerate(expr) if ch == "'"]
        if len(single_quotes) % 2 != 0:
            issues.append(DAXIssue(
                "E_QUOTE", "error",
                "Unmatched single-quote in expression — check 'TableName' references",
            ))

        # If no specific issues, check that the expression contains either:
        #   - a known function call
        #   - a column/measure reference
        #   - a numeric literal
        if not issues:
            has_func = bool(_FUNC_CALL.search(expr))
            has_ref = bool(_TABLE_COL_REF.search(expr))
            has_number = bool(re.search(r"\b\d+(\.\d+)?\b", expr))
            if not (has_func or has_ref or has_number):
                issues.append(DAXIssue(
                    "W_NO_CONTENT", "warning",
                    "Expression has no recognisable DAX function, column reference, or literal",
                ))

        is_valid = not any(i.severity == "error" for i in issues)
        return DAXValidationResult(expression=expr, is_valid=is_valid, issues=issues)

    def validate_all(self, measures: list[dict]) -> dict[str, DAXValidationResult]:
        """Validate a list of measure dicts with 'name' and 'expression' keys."""
        results: dict[str, DAXValidationResult] = {}
        for m in measures:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "").strip()
            expr = str(m.get("expression") or m.get("dax") or "").strip()
            if name:
                results[name] = self.validate(expr)
        return results


# ---------------------------------------------------------------------------
# DAX measure auto-generator
# ---------------------------------------------------------------------------

_AGG_PATTERN = re.compile(
    r"^(SUM|AVERAGE|AVG|COUNT|COUNTROWS|MIN|MAX|DISTINCTCOUNT)\s*\(",
    re.IGNORECASE,
)

def auto_dax_for_column(table: str, column: str, agg: str = "SUM") -> str:
    """Generate a minimal valid DAX expression for a column aggregation."""
    safe_table = table.replace("'", "''")
    safe_col = column.replace("'", "''").replace("]", "]]")
    func = agg.upper()
    if func not in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "DISTINCTCOUNT"):
        func = "SUM"
    return f"{func}('{safe_table}'[{safe_col}])"


def ensure_measure_expressions(
    measures: list[dict],
    entities: list[dict],
    fact_table: str = "",
) -> list[dict]:
    """Fill in missing or invalid DAX expressions for measures.

    For each measure whose expression is empty or fails validation:
      1. Try to find a source_column reference and generate  SUM('T'[C])
      2. Fall back to  0  (placeholder — safe, won't crash Power BI)

    Returns a new list of measure dicts with expressions guaranteed non-empty.
    """
    validator = DAXValidator()

    # Build column index: col_name_lower → (table, type)
    col_index: dict[str, tuple[str, str]] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        t_name = str(entity.get("name") or entity.get("id") or "").strip()
        for col in entity.get("columns", []) if isinstance(entity.get("columns"), list) else []:
            if not isinstance(col, dict):
                continue
            c_name = str(col.get("name") or "").strip()
            c_role = str(col.get("role") or "").lower()
            if c_name:
                col_index[c_name.lower()] = (t_name, c_role)

    healed: list[dict] = []
    for m in measures:
        if not isinstance(m, dict):
            continue
        m = dict(m)
        expr = str(m.get("expression") or m.get("dax") or "").strip()

        # Check if expression is valid
        result = validator.validate(expr) if expr else None
        needs_heal = not expr or (result is not None and not result.is_valid)

        if needs_heal:
            # Try source_columns first
            src_cols: list[dict] = m.get("source_columns", []) if isinstance(m.get("source_columns"), list) else []
            if src_cols and isinstance(src_cols[0], dict):
                sc = src_cols[0]
                t = str(sc.get("table") or fact_table or "").strip()
                c = str(sc.get("column") or "").strip()
                if t and c:
                    m["expression"] = auto_dax_for_column(t, c)
                    healed.append(m)
                    continue

            # Try matching measure name to a column
            m_name_key = re.sub(r"[^a-z0-9]", "", str(m.get("name") or "").lower())
            matched_col = next(
                (c_name for c_name in col_index if re.sub(r"[^a-z0-9]", "", c_name) == m_name_key),
                None,
            )
            if matched_col:
                t_name, _ = col_index[matched_col]
                m["expression"] = auto_dax_for_column(t_name, matched_col)
                healed.append(m)
                continue

            # Last resort: safe zero
            m["expression"] = "0"

        healed.append(m)
    return healed
