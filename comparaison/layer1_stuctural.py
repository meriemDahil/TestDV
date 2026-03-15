"""
Layer 1 – Structural Validation

Gate layer: if ANY check here fails, the engine stops.
No severity — every failure is blocking.

Checks:
  1. Column count
  2. Column names (missing / extra)
  3. Data type compatibility
  4. Null counts per column
  5. Column order (only if config.check_column_order = True)
"""
from __future__ import annotations

import time

from core.models import CheckResult, LayerResult, Status, ValidationConfig
from core.normalize import SQLiteLoader, SOURCE_TABLE, TARGET_TABLE

LAYER = "1_structural"


def run(db: SQLiteLoader, config: ValidationConfig) -> LayerResult:
    t0 = time.perf_counter()
    result = LayerResult(layer_name=LAYER, status=Status.PASSED)

    src_cols  = db.get_columns(SOURCE_TABLE)
    tgt_cols  = db.get_columns(TARGET_TABLE)
    src_types = db.get_dtypes(SOURCE_TABLE)
    tgt_types = db.get_dtypes(TARGET_TABLE)
    src_set   = set(src_cols)
    tgt_set   = set(tgt_cols)

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
            f"Column count mismatch: {config.source_label}={len(src_cols)}, "
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

    # Stop here — further type / null checks are meaningless if columns don't align
    if not result.passed:
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    common_cols = src_set & tgt_set

    # ── 3. Data type compatibility ───────────────────────────────────
    t = time.perf_counter()
    mismatches: dict = {}
    for col in common_cols:
        sf = _type_family(src_types.get(col, ""))
        tf = _type_family(tgt_types.get(col, ""))
        if sf != tf:
            mismatches[col] = {
                "source_type": src_types.get(col),
                "target_type": tgt_types.get(col),
            }
    passed = not mismatches
    result.add(CheckResult(
        layer=LAYER,
        check_name="data_types",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "All column data types are compatible."
            if passed else
            f"Type mismatch in {len(mismatches)} column(s): {list(mismatches.keys())}."
        ),
        details={"type_mismatches": mismatches},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 4. Null count per column ─────────────────────────────────────
    t = time.perf_counter()
    null_issues: dict = {}
    for col in common_cols:
        src_nulls = db.scalar(f'SELECT COUNT(*) FROM {SOURCE_TABLE} WHERE "{col}" IS NULL')
        tgt_nulls = db.scalar(f'SELECT COUNT(*) FROM {TARGET_TABLE} WHERE "{col}" IS NULL')
        if src_nulls != tgt_nulls:
            null_issues[col] = {"source_nulls": src_nulls, "target_nulls": tgt_nulls}
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


# ─────────────────────────────────────────────
# SQLite type families
# ─────────────────────────────────────────────

_FAMILIES: dict[str, set[str]] = {
    "integer": {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "INT2", "INT8"},
    "float":   {"REAL", "DOUBLE", "FLOAT", "NUMERIC", "DECIMAL"},
    "text":    {"TEXT", "VARCHAR", "NVARCHAR", "CHAR", "CLOB", "NCHAR", ""},
    "blob":    {"BLOB"},
    "boolean": {"BOOLEAN"},
}


def _type_family(dtype: str) -> str:
    d = dtype.upper().split("(")[0].strip()
    for family, members in _FAMILIES.items():
        if d in members:
            return family
    # SQLite type affinity: TEXT is the fallback
    return "text"