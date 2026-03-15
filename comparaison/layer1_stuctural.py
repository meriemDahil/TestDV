"""
Layer 1 – Structural Validation  (pure pandas)

Gate layer: if ANY check fails, the engine stops immediately.

Checks:
  1. Column count
  2. Column names (missing / extra)
  3. Data type compatibility
  4. Null counts per column
  5. Column order (only if config.check_column_order = True)
"""
from __future__ import annotations

import time

import pandas as pd

from core.models import CheckResult, LayerResult, Status, ValidationConfig

LAYER = "1_structural"


def run(src: pd.DataFrame, tgt: pd.DataFrame, config: ValidationConfig) -> LayerResult:
    t0 = time.perf_counter()
    result = LayerResult(layer_name=LAYER, status=Status.PASSED)

    src_cols = list(src.columns)
    tgt_cols = list(tgt.columns)
    src_set  = set(src_cols)
    tgt_set  = set(tgt_cols)

    # ── 1. Column count ──────────────────────────────────────────────
    t = time.perf_counter()
    passed = len(src_cols) == len(tgt_cols)
    result.add(CheckResult(
        layer=LAYER,
        check_name="column_count",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"Both datasets have {len(src_cols)} columns."
            if passed else
            f"Column count mismatch: "
            f"{config.source_label}={len(src_cols)}, "
            f"{config.target_label}={len(tgt_cols)}."
        ),
        details={"source_count": len(src_cols), "target_count": len(tgt_cols)},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 2. Column names ──────────────────────────────────────────────
    t = time.perf_counter()
    missing_in_target = sorted(src_set - tgt_set)
    extra_in_target   = sorted(tgt_set - src_set)
    passed = not missing_in_target and not extra_in_target
    result.add(CheckResult(
        layer=LAYER,
        check_name="column_names",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "All column names match."
            if passed else
            f"Missing in {config.target_label}: {missing_in_target}. "
            f"Unexpected in {config.target_label}: {extra_in_target}."
        ),
        details={
            "missing_in_target": missing_in_target,
            "extra_in_target":   extra_in_target,
        },
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # Stop early — type/null checks are meaningless with different columns
    if not result.passed:
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    common = src_set & tgt_set

    # ── 3. Data type compatibility ───────────────────────────────────
    t = time.perf_counter()
    mismatches: dict = {}
    for col in common:
        sf = _dtype_family(src[col].dtype)
        tf = _dtype_family(tgt[col].dtype)
        if sf != tf:
            mismatches[col] = {
                "source_dtype": str(src[col].dtype),
                "target_dtype": str(tgt[col].dtype),
            }
    passed = not mismatches
    result.add(CheckResult(
        layer=LAYER,
        check_name="data_types",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "All column dtypes are compatible."
            if passed else
            f"Dtype mismatch in {len(mismatches)} column(s): {list(mismatches.keys())}."
        ),
        details={"mismatches": mismatches},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 4. Null counts per column ────────────────────────────────────
    t = time.perf_counter()
    null_issues: dict = {}
    for col in common:
        sn = int(src[col].isna().sum())
        tn = int(tgt[col].isna().sum())
        if sn != tn:
            null_issues[col] = {"source_nulls": sn, "target_nulls": tn}
    passed = not null_issues
    result.add(CheckResult(
        layer=LAYER,
        check_name="null_counts",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "Null counts match for all columns."
            if passed else
            f"Null count mismatch in {len(null_issues)} column(s)."
        ),
        details={"null_issues": null_issues},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 5. Column order (optional) ───────────────────────────────────
    if config.check_column_order:
        t = time.perf_counter()
        passed = src_cols == tgt_cols
        result.add(CheckResult(
            layer=LAYER,
            check_name="column_order",
            status=Status.PASSED if passed else Status.FAILED,
            message=(
                "Column order is identical."
                if passed else
                "Column order differs between datasets."
            ),
            details={"source_order": src_cols, "target_order": tgt_cols},
            duration_ms=(time.perf_counter() - t) * 1000,
        ))

    result.duration_ms = (time.perf_counter() - t0) * 1000
    return result


# pandas dtype families

def _dtype_family(dtype) -> str:
    kind = dtype.kind   # 'i'=int, 'u'=uint, 'f'=float, 'O'=object, 'b'=bool, 'M'=datetime
    return {
        "i": "integer",
        "u": "integer",
        "f": "float",
        "O": "string",
        "b": "boolean",
        "M": "datetime",
        "m": "timedelta",
    }.get(kind, str(dtype))