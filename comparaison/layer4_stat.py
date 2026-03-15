"""
Layer 4 – Statistical / Distribution Validation  (pure pandas)

All stats computed directly on the DataFrame — no SQL, no external libs.

Checks:
  - Per-column: min, max, mean, stddev, percentiles (P50/P95/P99)
  - Null ratio per column
  - Cardinality (distinct count)
  - Distribution confidence score
"""
from __future__ import annotations

import time

import pandas as pd

from core.models import CheckResult, LayerResult, Status, ValidationConfig

LAYER = "4_statistical"


def run(src: pd.DataFrame, tgt: pd.DataFrame, config: ValidationConfig) -> LayerResult:
    t0 = time.perf_counter()
    result = LayerResult(layer_name=LAYER, status=Status.PASSED)

    common_cols  = sorted(set(src.columns) & set(tgt.columns))
    numeric_cols = [
        c for c in common_cols
        if pd.api.types.is_numeric_dtype(src[c]) or pd.api.types.is_numeric_dtype(tgt[c])
    ]

    if not numeric_cols:
        result.add(CheckResult(
            layer=LAYER,
            check_name="statistical_profile",
            status=Status.SKIPPED,
            message="No numeric columns — statistical checks skipped.",
            skipped_reason="Statistical validation only applies to numeric columns.",
        ))
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    # ── Per-column stat profile ──────────────────────────────────────
    t = time.perf_counter()
    all_issues:   list[dict] = []
    col_profiles: dict       = {}

    for col in numeric_cols:
        ss = pd.to_numeric(src[col], errors="coerce")
        ts = pd.to_numeric(tgt[col], errors="coerce")

        src_stats = _compute_stats(ss, len(src), config.percentiles)
        tgt_stats = _compute_stats(ts, len(tgt), config.percentiles)
        col_issues = _compare_stats(col, src_stats, tgt_stats, config.stat_tolerance)

        all_issues.extend(col_issues)
        col_profiles[col] = {
            "source": src_stats,
            "target": tgt_stats,
            "issues": col_issues,
        }

    passed = not all_issues
    result.add(CheckResult(
        layer=LAYER,
        check_name="per_column_statistics",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"Statistical profiles match for all {len(numeric_cols)} numeric column(s)."
            if passed else
            f"Statistical deviation in {len(all_issues)} stat(s)."
        ),
        details={
            "tolerance": config.stat_tolerance,
            "columns_checked": numeric_cols,
            "column_profiles": col_profiles,
            "issues": all_issues,
        },
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── Null ratio ───────────────────────────────────────────────────
    t = time.perf_counter()
    null_issues = _check_null_ratios(src, tgt, common_cols, config.stat_tolerance)
    passed = not null_issues
    result.add(CheckResult(
        layer=LAYER,
        check_name="null_ratio",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "Null ratios match for all columns."
            if passed else
            f"Null ratio mismatch in {len(null_issues)} column(s)."
        ),
        details={"issues": null_issues},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── Cardinality ──────────────────────────────────────────────────
    t = time.perf_counter()
    card_issues = _check_cardinality(src, tgt, common_cols, config.stat_tolerance)
    passed = not card_issues
    result.add(CheckResult(
        layer=LAYER,
        check_name="cardinality",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "Cardinality matches for all columns."
            if passed else
            f"Cardinality mismatch in {len(card_issues)} column(s)."
        ),
        details={"issues": card_issues},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── Distribution confidence score ────────────────────────────────
    t = time.perf_counter()
    score = _confidence_score(col_profiles)
    passed = score >= 0.95
    result.add(CheckResult(
        layer=LAYER,
        check_name="distribution_confidence",
        status=Status.PASSED if passed else Status.FAILED,
        message=f"Distribution confidence: {score:.1%} ({'≥' if passed else '<'} 95% threshold).",
        details={"score": round(score, 4), "threshold": 0.95},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    result.duration_ms = (time.perf_counter() - t0) * 1000
    return result

# Helpers

def _compute_stats(series: pd.Series, total_rows: int, percentiles: list[float]) -> dict:
    clean = series.dropna()
    stats: dict = {
        "min":        float(clean.min())  if len(clean) else None,
        "max":        float(clean.max())  if len(clean) else None,
        "mean":       float(clean.mean()) if len(clean) else None,
        "stddev":     float(clean.std())  if len(clean) else None,
        "non_null":   int(len(clean)),
        "null_ratio": round(1 - len(clean) / total_rows, 6) if total_rows else 0,
    }
    for p in percentiles:
        stats[f"p{int(p * 100)}"] = float(clean.quantile(p)) if len(clean) else None
    return stats


def _compare_stats(col: str, src: dict, tgt: dict, tolerance: float) -> list[dict]:
    issues = []
    keys = ["min", "max", "mean", "stddev"] + [k for k in src if k.startswith("p")]
    for key in keys:
        sv, tv = src.get(key), tgt.get(key)
        if sv is None or tv is None:
            continue
        try:
            base     = abs(float(sv)) if float(sv) != 0 else 1.0
            rel_diff = abs(float(sv) - float(tv)) / base
            if rel_diff > tolerance:
                issues.append({
                    "column": col, "stat": key,
                    "source": sv, "target": tv,
                    "relative_diff": round(rel_diff, 6),
                    "tolerance": tolerance,
                })
        except (TypeError, ValueError):
            pass
    return issues


def _check_null_ratios(
    src: pd.DataFrame, tgt: pd.DataFrame,
    cols: list[str], tolerance: float,
) -> list[dict]:
    issues = []
    for col in cols:
        sr = src[col].isna().mean()
        tr = tgt[col].isna().mean()
        if abs(sr - tr) > tolerance:
            issues.append({
                "column": col,
                "source_null_ratio": round(float(sr), 6),
                "target_null_ratio": round(float(tr), 6),
                "delta": round(abs(float(sr) - float(tr)), 6),
            })
    return issues


def _check_cardinality(
    src: pd.DataFrame, tgt: pd.DataFrame,
    cols: list[str], tolerance: float,
) -> list[dict]:
    issues = []
    for col in cols:
        sc = src[col].nunique()
        tc = tgt[col].nunique()
        if sc != tc:
            base = max(sc, 1)
            if abs(sc - tc) / base > tolerance:
                issues.append({
                    "column": col,
                    "source_cardinality": int(sc),
                    "target_cardinality": int(tc),
                    "delta": int(abs(sc - tc)),
                })
    return issues


def _confidence_score(col_profiles: dict) -> float:
    total, ok = 0, 0
    for data in col_profiles.values():
        n_stats  = len([k for k in data.get("source", {}) if k not in ("non_null", "null_ratio")])
        n_issues = len(data.get("issues", []))
        total += n_stats
        ok    += max(0, n_stats - n_issues)
    return ok / total if total > 0 else 1.0