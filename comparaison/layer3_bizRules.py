"""
Layer 3 – Business Rule Validation  (pure pandas)

Checks:
  1. Grand total aggregations (SUM / COUNT / AVG)  — SKIPPED if not configured
  2. Float tolerance per column
  3. Group-by consistency                          — SKIPPED if not configured
  4. Custom callable rules                         — SKIPPED if none defined
"""
from __future__ import annotations

import time
from typing import Any
import pandas as pd
from core.models import BusinessRule, CheckResult, LayerResult, Status, ValidationConfig, ColumnTolerance

LAYER = "3_business_rules"


def run(src: pd.DataFrame, tgt: pd.DataFrame, config: ValidationConfig) -> LayerResult:
    t0 = time.perf_counter()
    result = LayerResult(layer_name=LAYER, status=Status.PASSED)

    numeric_cols = list(src.select_dtypes(include="number").columns)
    tol_map = {t.column: t for t in config.column_tolerances}

    # ── 1. Grand total aggregations ──────────────────────────────────
    if not config.aggregation_columns:
        result.add(CheckResult(
            layer=LAYER,
            check_name="grand_total_aggregations",
            status=Status.SKIPPED,
            message="Grand total aggregations skipped: no aggregation_columns configured.",
            skipped_reason=(
                "Set aggregation_columns in ValidationConfig. "
                "Example: aggregation_columns=['revenue', 'qty']"
            ),
        ))
    else:
        t = time.perf_counter()
        agg_issues = _check_aggregations(src, tgt, config)
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

    # ── 2. Float tolerance ───────────────────────────────────────────
    if not numeric_cols:
        result.add(CheckResult(
            layer=LAYER,
            check_name="float_tolerance",
            status=Status.SKIPPED,
            message="Float tolerance skipped: no numeric columns detected.",
            skipped_reason="This check runs automatically when numeric columns are present.",
        ))
    else:
        t = time.perf_counter()
        float_issues = _check_float_tolerance(src, tgt, numeric_cols, tol_map, config)
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

    # ── 3. Group-by consistency ──────────────────────────────────────
    if not config.group_by_columns:
        result.add(CheckResult(
            layer=LAYER,
            check_name="group_by_consistency",
            status=Status.SKIPPED,
            message="Group-by consistency skipped: no group_by_columns configured.",
            skipped_reason=(
                "Set group_by_columns and aggregation_columns. "
                "Example: group_by_columns=['region'], aggregation_columns=['revenue']"
            ),
        ))
    elif not config.aggregation_columns:
        result.add(CheckResult(
            layer=LAYER,
            check_name="group_by_consistency",
            status=Status.SKIPPED,
            message="Group-by skipped: aggregation_columns is empty.",
            skipped_reason="Set aggregation_columns alongside group_by_columns.",
        ))
    else:
        t = time.perf_counter()
        group_issues = _check_group_by(src, tgt, config)
        passed = not group_issues
        result.add(CheckResult(
            layer=LAYER,
            check_name="group_by_consistency",
            status=Status.PASSED if passed else Status.FAILED,
            message=(
                f"Group-by sums match on {config.group_by_columns}."
                if passed else
                f"Group-by mismatch in {len(group_issues)} group(s)."
            ),
            details={"issues": group_issues},
            duration_ms=(time.perf_counter() - t) * 1000,
        ))

    # ── 4. Custom rules ──────────────────────────────────────────────
    if not config.business_rules:
        result.add(CheckResult(
            layer=LAYER,
            check_name="custom_rules",
            status=Status.SKIPPED,
            message="No custom business rules defined.",
            skipped_reason=(
                "Add BusinessRule entries to ValidationConfig.business_rules. "
                "Example: BusinessRule(name='min_revenue', rule_fn=lambda df: df['revenue'].min())"
            ),
        ))
    else:
        for rule in config.business_rules:
            t = time.perf_counter()
            check = _evaluate_rule(src, tgt, rule)
            check.duration_ms = (time.perf_counter() - t) * 1000
            result.add(check)

    result.duration_ms = (time.perf_counter() - t0) * 1000
    return result


# Helpers

def _check_aggregations(
    src: pd.DataFrame,
    tgt: pd.DataFrame,
    config: ValidationConfig,
) -> list[dict]:
    issues = []
    for col in config.aggregation_columns:
        if col not in src.columns or col not in tgt.columns:
            issues.append({"column": col, "error": "column not found"})
            continue
        for fn_name, fn in [("SUM", "sum"), ("COUNT", "count"), ("AVG", "mean")]:
            sv = getattr(pd.to_numeric(src[col], errors="coerce"), fn)()
            tv = getattr(pd.to_numeric(tgt[col], errors="coerce"), fn)()
            if not _within_tolerance(sv, tv, config.default_abs_tolerance, config.default_rel_tolerance):
                issues.append({
                    "column": col, "function": fn_name,
                    "source": float(sv), "target": float(tv),
                    "delta": float(sv) - float(tv),
                })
    return issues


def _check_float_tolerance(
    src: pd.DataFrame,
    tgt: pd.DataFrame,
    numeric_cols: list[str],
    tol_map: dict[str, ColumnTolerance],
    config: ValidationConfig,
) -> list[dict]:
    """
    Compares numeric columns value-by-value after sorting.
    Note: without a primary_key this is position-based (sorted order),
    not key-aligned. Set primary_key for exact row matching.
    """
    issues = []
    for col in numeric_cols:
        if col not in tgt.columns:
            continue
        tol = tol_map.get(col)
        abs_tol = tol.absolute if tol else config.default_abs_tolerance
        rel_tol = tol.relative if tol else config.default_rel_tolerance

        sv = pd.to_numeric(src[col], errors="coerce").dropna().sort_values().reset_index(drop=True)
        tv = pd.to_numeric(tgt[col], errors="coerce").dropna().sort_values().reset_index(drop=True)

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
    return issues


def _check_group_by(
    src: pd.DataFrame,
    tgt: pd.DataFrame,
    config: ValidationConfig,
) -> list[dict]:
    issues = []
    grp  = config.group_by_columns
    agg  = [c for c in config.aggregation_columns if c in src.columns and c in tgt.columns]
    if not agg:
        return []

    try:
        src_grp = src.groupby(grp)[agg].sum().reset_index().sort_values(grp).reset_index(drop=True)
        tgt_grp = tgt.groupby(grp)[agg].sum().reset_index().sort_values(grp).reset_index(drop=True)

        if len(src_grp) != len(tgt_grp):
            issues.append({
                "issue": "group_count_mismatch",
                "source_groups": len(src_grp),
                "target_groups": len(tgt_grp),
            })
            return issues

        merged = src_grp.merge(tgt_grp, on=grp, suffixes=("_src", "_tgt"))
        for col in agg:
            bad = merged[(merged[f"{col}_src"] - merged[f"{col}_tgt"]).abs() > config.default_abs_tolerance]
            if not bad.empty:
                issues.append({
                    "column": col,
                    "mismatched_groups": bad[grp].to_dict(orient="records")[:10],
                })
    except Exception as e:
        issues.append({"error": str(e)})

    return issues


def _evaluate_rule(
    src: pd.DataFrame,
    tgt: pd.DataFrame,
    rule: "BusinessRule",
) -> CheckResult:
    try:
        sv = rule.rule_fn(src)
        tv = rule.rule_fn(tgt)
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
            details={"source": sv, "target": tv},
        )
    except Exception as e:
        return CheckResult(
            layer=LAYER,
            check_name=f"rule_{rule.name}",
            status=Status.FAILED,
            message=f"Rule '{rule.name}' raised an exception: {e}.",
            details={"error": str(e)},
        )


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