"""
Layer 2 – Data-Level Validation  (pure pandas)

Checks:
  1. Row count
  2. Full dataset hash  (order-independent SHA-256)
  3. Column-level hash
  4. Duplicate row count
  5. Bucketed checksum  (no PK needed)
  6. Row-aligned diff   (SKIPPED if no primary_key configured)
"""
from __future__ import annotations

import hashlib
import time

import pandas as pd

from core.models import CheckResult, LayerResult, Status, ValidationConfig

LAYER = "2_data_level"


def run(src: pd.DataFrame, tgt: pd.DataFrame, config: ValidationConfig) -> LayerResult:
    t0 = time.perf_counter()
    result = LayerResult(layer_name=LAYER, status=Status.PASSED)

    common_cols = sorted(set(src.columns) & set(tgt.columns))

    # ── 1. Row count ─────────────────────────────────────────────────
    t = time.perf_counter()
    passed = len(src) == len(tgt)
    result.add(CheckResult(
        layer=LAYER,
        check_name="row_count",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"Row counts match: {len(src):,} rows."
            if passed else
            f"Row count mismatch: "
            f"{config.source_label}={len(src):,}, "
            f"{config.target_label}={len(tgt):,} "
            f"(delta={abs(len(src) - len(tgt)):,})."
        ),
        details={
            "source_rows": len(src),
            "target_rows": len(tgt),
            "delta": len(src) - len(tgt),
        },
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    if not result.passed:
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    # ── 2. Full dataset hash ─────────────────────────────────────────
    t = time.perf_counter()
    src_hash = _dataset_hash(src, common_cols)
    tgt_hash = _dataset_hash(tgt, common_cols)
    passed = src_hash == tgt_hash
    result.add(CheckResult(
        layer=LAYER,
        check_name="full_dataset_hash",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "Full dataset hash matches — datasets are identical."
            if passed else
            "Full dataset hash mismatch — content differs."
        ),
        details={"source_hash": src_hash, "target_hash": tgt_hash},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 3. Column-level hash ─────────────────────────────────────────
    t = time.perf_counter()
    col_results: dict = {}
    mismatched: list[str] = []
    for col in common_cols:
        sh = _column_hash(src[col])
        th = _column_hash(tgt[col])
        match = sh == th
        col_results[col] = {"match": match, "source_hash": sh, "target_hash": th}
        if not match:
            mismatched.append(col)
    passed = not mismatched
    result.add(CheckResult(
        layer=LAYER,
        check_name="column_level_hash",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"All {len(common_cols)} column hashes match."
            if passed else
            f"Hash mismatch in {len(mismatched)} column(s): {mismatched}."
        ),
        details={"column_hashes": col_results, "mismatched_columns": mismatched},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 4. Duplicate rows ────────────────────────────────────────────
    t = time.perf_counter()
    src_dups = int(src.duplicated().sum())
    tgt_dups = int(tgt.duplicated().sum())
    passed = src_dups == tgt_dups
    result.add(CheckResult(
        layer=LAYER,
        check_name="duplicate_rows",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"Duplicate row counts match ({src_dups:,})."
            if passed else
            f"Duplicate count mismatch: "
            f"{config.source_label}={src_dups:,}, "
            f"{config.target_label}={tgt_dups:,}."
        ),
        details={"source_duplicates": src_dups, "target_duplicates": tgt_dups},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 5. Bucketed checksum ─────────────────────────────────────────
    t = time.perf_counter()
    bucket_issues = _bucketed_checksum(src, tgt, common_cols, bucket_count=10)
    passed = not bucket_issues
    result.add(CheckResult(
        layer=LAYER,
        check_name="bucketed_checksum",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "All 10 bucket checksums match."
            if passed else
            f"Checksum mismatch in {len(bucket_issues)} bucket(s)."
        ),
        details={"mismatched_buckets": bucket_issues},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 6. Row-aligned diff ──────────────────────────────────────────
    t = time.perf_counter()
    if not config.primary_key:
        result.add(CheckResult(
            layer=LAYER,
            check_name="row_aligned_diff",
            status=Status.SKIPPED,
            message="Row-aligned diff skipped: no primary_key configured.",
            skipped_reason=(
                "Set primary_key in ValidationConfig to enable cell-by-cell row comparison. "
                "Example: primary_key=['order_id']. "
                "Without a key we have no way to match rows between both datasets."
            ),
            duration_ms=(time.perf_counter() - t) * 1000,
        ))
    else:
        missing_pk = [c for c in config.primary_key if c not in set(src.columns)]
        if missing_pk:
            result.add(CheckResult(
                layer=LAYER,
                check_name="row_aligned_diff",
                status=Status.FAILED,
                message=f"primary_key columns not found in source: {missing_pk}.",
                details={"missing_pk_cols": missing_pk},
                duration_ms=(time.perf_counter() - t) * 1000,
            ))
        else:
            diff = _row_aligned_diff(src, tgt, common_cols, config.primary_key, limit=20)
            passed = diff.empty
            result.add(CheckResult(
                layer=LAYER,
                check_name="row_aligned_diff",
                status=Status.PASSED if passed else Status.FAILED,
                message=(
                    "No row-level differences found."
                    if passed else
                    f"{len(diff)} differing row(s) found (showing up to 20)."
                ),
                details={"diff_sample": diff.to_dict(orient="records")},
                duration_ms=(time.perf_counter() - t) * 1000,
            ))

    result.duration_ms = (time.perf_counter() - t0) * 1000
    return result


# Hashing helpers — all pure Python / pandas

def _dataset_hash(df: pd.DataFrame, cols: list[str]) -> str:
    """
    Order-independent SHA-256 of the full dataset.
    Rows are sorted before hashing — safe against engines
    that return rows in different sequences.
    """
    subset = df[sorted(cols)].copy()
    subset = subset.fillna("__NULL__").astype(str)
    subset_sorted = subset.sort_values(by=list(subset.columns)).reset_index(drop=True)
    content = subset_sorted.to_csv(index=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _column_hash(series: pd.Series) -> str:
    """Sorted, null-safe hash of a single column."""
    cleaned = series.fillna("__NULL__").astype(str).sort_values()
    content = "\n".join(cleaned).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _bucketed_checksum(
    src: pd.DataFrame,
    tgt: pd.DataFrame,
    cols: list[str],
    bucket_count: int = 10,
) -> list[dict]:
    """
    Splits both DataFrames into N sorted buckets and compares
    each bucket's MD5. Localises which portion of the data differs.
    """
    src_s = src[cols].fillna("__NULL__").astype(str).sort_values(by=cols).reset_index(drop=True)
    tgt_s = tgt[cols].fillna("__NULL__").astype(str).sort_values(by=cols).reset_index(drop=True)

    mismatches = []
    for b in range(bucket_count):
        sb = src_s[src_s.index % bucket_count == b]
        tb = tgt_s[tgt_s.index % bucket_count == b]
        sh = hashlib.md5(sb.to_csv(index=False).encode()).hexdigest()
        th = hashlib.md5(tb.to_csv(index=False).encode()).hexdigest()
        if sh != th:
            mismatches.append({
                "bucket": b,
                "source_hash": sh,
                "target_hash": th,
                "source_rows": len(sb),
                "target_rows": len(tb),
            })
    return mismatches


def _row_aligned_diff(
    src: pd.DataFrame,
    tgt: pd.DataFrame,
    cols: list[str],
    pk_cols: list[str],
    limit: int = 20,
) -> pd.DataFrame:
    """
    Joins source and target on the primary key.
    Returns rows where any non-PK column differs.
    """
    merged = src[cols].merge(tgt[cols], on=pk_cols, suffixes=("_src", "_tgt"))
    non_pk = [c for c in cols if c not in pk_cols]

    diff_mask = pd.Series(False, index=merged.index)
    for col in non_pk:
        sc, tc = f"{col}_src", f"{col}_tgt"
        if sc in merged.columns and tc in merged.columns:
            diff_mask |= merged[sc].astype(str) != merged[tc].astype(str)

    return merged[diff_mask].head(limit)