"""
Layer 3 – Business Rule Validation

Checks:
  1. Grand total aggregations (SUM / COUNT / AVG) — global
  2. Float tolerance per column
  3. Group-by consistency  ← SKIPPED if group_by_columns not configured
  4. Custom SQL rules      ← skipped if none defined

SKIPPED checks are surfaced clearly — we tell you what config you'd need.
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

from core.models import BusinessRule, CheckResult, LayerResult, Status, ValidationConfig, ColumnTolerance
from core.normalize import SQLiteLoader, SOURCE_TABLE, TARGET_TABLE

LAYER = "3_business_rules"


def run(db: SQLiteLoader, config: ValidationConfig) -> LayerResult:
    t0 = time.perf_counter()
    result = LayerResult(layer_name=LAYER, status=Status.PASSED)

    numeric_cols = _get_numeric_cols(db)
    tol_map = {t.column: t for t in config.column_tolerances}

    # ── 1. Grand total aggregations ──────────────────────────────────
    if config.aggregation_columns:
        t = time.perf_counter()
        agg_issues = _check_global_aggregations(db, config)
        passed = not agg_issues
        result.add(CheckResult(
            layer=LAYER,
            check_name="grand_total_aggregations",
            status=Status.PASSED if passed else Status.FAILED,
            message=(
                f"Global SUM/COUNT/AVG match for all {len(config.aggregation_columns)} column(s)."
                if passed else
                f"Aggregation mismatch in {len(agg_issues)} case(s)."
            ),
            details={"issues": agg_issues},
            duration_ms=(time.perf_counter() - t) * 1000,
        ))
    else:
        result.add(CheckResult(
            layer=LAYER,
            check_name="grand_total_aggregations",
            status=Status.SKIPPED,
            message="Grand total aggregations skipped: no aggregation_columns configured.",
            skipped_reason=(
                "Set aggregation_columns in ValidationConfig to enable this check. "
                "Example: aggregation_columns=['revenue', 'qty']"
            ),
        ))

    # ── 2. Float / tolerance column comparison ───────────────────────
    if numeric_cols:
        t = time.perf_counter()
        float_issues = _check_float_tolerance(db, numeric_cols, tol_map, config)
        passed = not float_issues
        result.add(CheckResult(
            layer=LAYER,
            check_name="float_tolerance",
            status=Status.PASSED if passed else Status.FAILED,
            message=(
                f"All {len(numeric_cols)} numeric column(s) within tolerance."
                if passed else
                f"{len(float_issues)} numeric column(s) exceed tolerance."
            ),
            details={"issues": float_issues, "columns_checked": numeric_cols},
            duration_ms=(time.perf_counter() - t) * 1000,
        ))
    else:
        result.add(CheckResult(
            layer=LAYER,
            check_name="float_tolerance",
            status=Status.SKIPPED,
            message="Float tolerance check skipped: no numeric columns detected.",
            skipped_reason="This check runs automatically once numeric columns are present.",
        ))

    # ── 3. Group-by consistency ──────────────────────────────────────
    if not config.group_by_columns:
        result.add(CheckResult(
            layer=LAYER,
            check_name="group_by_consistency",
            status=Status.SKIPPED,
            message="Group-by consistency skipped: no group_by_columns configured.",
            skipped_reason=(
                "Set group_by_columns in ValidationConfig to enable per-group aggregation checks. "
                "Example: group_by_columns=['region', 'product']. "
                "Also requires aggregation_columns to be set."
            ),
        ))
    elif not config.aggregation_columns:
        result.add(CheckResult(
            layer=LAYER,
            check_name="group_by_consistency",
            status=Status.SKIPPED,
            message="Group-by consistency skipped: group_by_columns set but aggregation_columns is empty.",
            skipped_reason="Set aggregation_columns alongside group_by_columns.",
        ))
    else:
        t = time.perf_counter()
        group_issues = _check_group_by(db, config)
        passed = not group_issues
        result.add(CheckResult(
            layer=LAYER,
            check_name="group_by_consistency",
            status=Status.PASSED if passed else Status.FAILED,
            message=(
                f"Group-by sums match for all groups on {config.group_by_columns}."
                if passed else
                f"Group-by mismatch in {len(group_issues)} group(s)."
            ),
            details={"issues": group_issues},
            duration_ms=(time.perf_counter() - t) * 1000,
        ))

    # ── 4. Custom business rules ─────────────────────────────────────
    if not config.business_rules:
        result.add(CheckResult(
            layer=LAYER,
            check_name="custom_rules",
            status=Status.SKIPPED,
            message="No custom business rules defined.",
            skipped_reason=(
                "Add BusinessRule entries to ValidationConfig.business_rules to enable. "
                "Example: BusinessRule(name='positive_revenue', expression=\"SELECT MIN(revenue) FROM {table}\")."
            ),
        ))
    else:
        for rule in config.business_rules:
            t = time.perf_counter()
            check = _evaluate_rule(db, rule)
            check.duration_ms = (time.perf_counter() - t) * 1000
            result.add(check)

    result.duration_ms = (time.perf_counter() - t0) * 1000
    return result


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _check_global_aggregations(db: SQLiteLoader, config: ValidationConfig) -> list[dict]:
    issues = []
    for col in config.aggregation_columns:
        for fn in ("SUM", "COUNT", "AVG"):
            try:
                sv = db.scalar(f'SELECT {fn}(CAST("{col}" AS REAL)) FROM {SOURCE_TABLE}')
                tv = db.scalar(f'SELECT {fn}(CAST("{col}" AS REAL)) FROM {TARGET_TABLE}')
                if not _within_tolerance(sv, tv, config.default_abs_tolerance, config.default_rel_tolerance):
                    issues.append({
                        "column": col, "function": fn,
                        "source": sv, "target": tv,
                        "delta": _safe_delta(sv, tv),
                    })
            except Exception as e:
                issues.append({"column": col, "function": fn, "error": str(e)})
    return issues


def _check_float_tolerance(
    db: SQLiteLoader,
    numeric_cols: list[str],
    tol_map: dict[str, ColumnTolerance],
    config: ValidationConfig,
) -> list[dict]:
    """
    Row-by-row comparison using pandas (SQLite has no ROW_NUMBER).
    Falls back to sorted merge — not PK-aligned (documented limitation).
    """
    issues = []
    src_df = db.dataframe(SOURCE_TABLE)
    tgt_df = db.dataframe(TARGET_TABLE)

    for col in numeric_cols:
        tol = tol_map.get(col)
        abs_tol = tol.absolute if tol else config.default_abs_tolerance
        rel_tol = tol.relative if tol else config.default_rel_tolerance

        try:
            sv = pd.to_numeric(src_df[col], errors="coerce").dropna().sort_values().reset_index(drop=True)
            tv = pd.to_numeric(tgt_df[col], errors="coerce").dropna().sort_values().reset_index(drop=True)

            if len(sv) != len(tv):
                issues.append({"column": col, "reason": "different non-null counts",
                                "source_count": len(sv), "target_count": len(tv)})
                continue

            diff = (sv - tv).abs()
            max_abs = float(diff.max())
            base = sv.abs().replace(0, 1)
            max_rel = float((diff / base).max())

            if max_abs > abs_tol or max_rel > rel_tol:
                issues.append({
                    "column": col,
                    "max_absolute_diff": max_abs,
                    "max_relative_diff": max_rel,
                    "abs_tolerance": abs_tol,
                    "rel_tolerance": rel_tol,
                })
        except Exception as e:
            issues.append({"column": col, "error": str(e)})
    return issues


def _check_group_by(db: SQLiteLoader, config: ValidationConfig) -> list[dict]:
    issues = []
    group_cols  = config.group_by_columns
    agg_cols    = config.aggregation_columns

    src_df = db.dataframe(SOURCE_TABLE)
    tgt_df = db.dataframe(TARGET_TABLE)

    agg_dict = {c: "sum" for c in agg_cols if c in src_df.columns}
    if not agg_dict:
        return []

    try:
        src_grp = src_df.groupby(group_cols)[list(agg_dict.keys())].sum().reset_index()
        tgt_grp = tgt_df.groupby(group_cols)[list(agg_dict.keys())].sum().reset_index()

        if len(src_grp) != len(tgt_grp):
            issues.append({
                "issue": "group_count_mismatch",
                "source_groups": len(src_grp),
                "target_groups": len(tgt_grp),
            })
            return issues

        merged = src_grp.merge(tgt_grp, on=group_cols, suffixes=("_src", "_tgt"))
        for col in agg_dict:
            bad_rows = merged[
                (merged[f"{col}_src"] - merged[f"{col}_tgt"]).abs()
                > config.default_abs_tolerance
            ]
            if not bad_rows.empty:
                issues.append({
                    "column": col,
                    "mismatched_groups": bad_rows[group_cols].to_dict(orient="records")[:10],
                })
    except Exception as e:
        issues.append({"error": str(e)})

    return issues


def _evaluate_rule(db: SQLiteLoader, rule: "BusinessRule") -> CheckResult:
    try:
        sv = db.scalar(rule.expression.replace("{table}", SOURCE_TABLE))
        tv = db.scalar(rule.expression.replace("{table}", TARGET_TABLE))
        passed = _within_tolerance(sv, tv, rule.tolerance, rule.tolerance)
        return CheckResult(
            layer=LAYER,
            check_name=f"rule_{rule.name}",
            status=Status.PASSED if passed else Status.FAILED,
            message=(
                f"Rule '{rule.name}' passed (source={sv}, target={tv})."
                if passed else
                f"Rule '{rule.name}' FAILED: source={sv}, target={tv}."
            ),
            details={"source": sv, "target": tv, "expression": rule.expression},
        )
    except Exception as e:
        return CheckResult(
            layer=LAYER,
            check_name=f"rule_{rule.name}",
            status=Status.FAILED,
            message=f"Rule '{rule.name}' raised an exception: {e}.",
            details={"expression": rule.expression, "error": str(e)},
        )


def _get_numeric_cols(db: SQLiteLoader) -> list[str]:
    NUMERIC = {"INTEGER", "INT", "REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL",
               "BIGINT", "INT2", "INT8", "TINYINT", "SMALLINT"}
    src_types = db.get_dtypes(SOURCE_TABLE)
    return [
        col for col, dtype in src_types.items()
        if dtype.upper().split("(")[0].strip() in NUMERIC
    ]


def _within_tolerance(a: Any, b: Any, abs_tol: float, rel_tol: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        fa, fb = float(a), float(b)
        diff = abs(fa - fb)
        if diff <= abs_tol:
            return True
        base = abs(fa) if fa != 0 else 1.0
        return (diff / base) <= rel_tol
    except (TypeError, ValueError):
        return str(a) == str(b)


def _safe_delta(a: Any, b: Any) -> Any:
    try:
        return float(a) - float(b)
    except (TypeError, ValueError):
        return None