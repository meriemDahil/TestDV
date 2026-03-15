"""
Layer 2 – Data-Level Validation

Checks:
  1. Row count
  2. Full dataset hash (order-independent, Python-side)
  3. Column-level hash (per column)
  4. Duplicate row count
  5. Row-aligned diff  ← SKIPPED if no primary_key configured
  6. Bucketed checksum ← bucket-by-hash, no PK needed

SKIPPED checks are surfaced clearly in the report so you know
exactly what extra information would unlock them.
"""
from __future__ import annotations

import hashlib
import time

import pandas as pd

from core.models import CheckResult, LayerResult, Status, ValidationConfig
from core.normalize import SQLiteLoader, SOURCE_TABLE, TARGET_TABLE

LAYER = "2_data_level"


def run(db: SQLiteLoader, config: ValidationConfig) -> LayerResult:
    t0 = time.perf_counter()
    result = LayerResult(layer_name=LAYER, status=Status.PASSED)

    src_cols = db.get_columns(SOURCE_TABLE)
    tgt_cols = db.get_columns(TARGET_TABLE)
    common_cols = sorted(set(src_cols) & set(tgt_cols))
    hash_cols = common_cols   # hash all shared columns

    src_count = db.row_count(SOURCE_TABLE)
    tgt_count = db.row_count(TARGET_TABLE)

    # ── 1. Row count ─────────────────────────────────────────────────
    t = time.perf_counter()
    passed = src_count == tgt_count
    result.add(CheckResult(
        layer=LAYER,
        check_name="row_count",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"Row counts match: {src_count:,} rows."
            if passed else
            f"Row count mismatch: {config.source_label}={src_count:,}, "
            f"{config.target_label}={tgt_count:,} "
            f"(delta={abs(src_count - tgt_count):,})."
        ),
        details={"source_rows": src_count, "target_rows": tgt_count,
                 "delta": src_count - tgt_count},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    if not result.passed:
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    # ── 2. Full dataset hash ─────────────────────────────────────────
    t = time.perf_counter()
    src_hash = db.dataset_hash(SOURCE_TABLE, hash_cols)
    tgt_hash = db.dataset_hash(TARGET_TABLE, hash_cols)
    passed = src_hash == tgt_hash
    result.add(CheckResult(
        layer=LAYER,
        check_name="full_dataset_hash",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            "Full dataset hash matches — datasets are byte-identical (after normalization)."
            if passed else
            "Full dataset hash mismatch — content differs."
        ),
        details={"source_hash": src_hash, "target_hash": tgt_hash},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 3. Column-level hash ─────────────────────────────────────────
    t = time.perf_counter()
    col_results: dict = {}
    mismatched_cols: list[str] = []
    for col in hash_cols:
        sh = db.column_hash(SOURCE_TABLE, col)
        th = db.column_hash(TARGET_TABLE, col)
        match = sh == th
        col_results[col] = {"match": match, "source_hash": sh, "target_hash": th}
        if not match:
            mismatched_cols.append(col)

    passed = not mismatched_cols
    result.add(CheckResult(
        layer=LAYER,
        check_name="column_level_hash",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"All {len(hash_cols)} column hashes match."
            if passed else
            f"Hash mismatch in {len(mismatched_cols)} column(s): {mismatched_cols}."
        ),
        details={"column_hashes": col_results, "mismatched_columns": mismatched_cols},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 4. Duplicate rows ────────────────────────────────────────────
    t = time.perf_counter()
    src_dups = _count_duplicates(db, SOURCE_TABLE, hash_cols)
    tgt_dups = _count_duplicates(db, TARGET_TABLE, hash_cols)
    passed = src_dups == tgt_dups
    result.add(CheckResult(
        layer=LAYER,
        check_name="duplicate_rows",
        status=Status.PASSED if passed else Status.FAILED,
        message=(
            f"Duplicate row counts match ({src_dups:,})."
            if passed else
            f"Duplicate count mismatch: {config.source_label}={src_dups:,}, "
            f"{config.target_label}={tgt_dups:,}."
        ),
        details={"source_duplicates": src_dups, "target_duplicates": tgt_dups},
        duration_ms=(time.perf_counter() - t) * 1000,
    ))

    # ── 5. Row-aligned diff — SKIPPED without a primary key ──────────
    t = time.perf_counter()
    if not config.primary_key:
        result.add(CheckResult(
            layer=LAYER,
            check_name="row_aligned_diff",
            status=Status.SKIPPED,
            message="Row-aligned diff skipped: no primary_key configured.",
            skipped_reason=(
                "To enable this check, set primary_key in ValidationConfig. "
                "Example: primary_key=['order_id']. "
                "Without a key we cannot join rows from both datasets to compare them cell-by-cell."
            ),
            duration_ms=(time.perf_counter() - t) * 1000,
        ))
    else:
        # Verify the configured PK columns actually exist
        missing_pk_cols = [c for c in config.primary_key if c not in set(src_cols)]
        if missing_pk_cols:
            result.add(CheckResult(
                layer=LAYER,
                check_name="row_aligned_diff",
                status=Status.FAILED,
                message=f"primary_key columns not found in source: {missing_pk_cols}.",
                details={"missing_pk_cols": missing_pk_cols},
                duration_ms=(time.perf_counter() - t) * 1000,
            ))
        else:
            diff = _row_aligned_diff(db, hash_cols, config.primary_key, limit=20)
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

    # ── 6. Bucketed checksum (no PK needed) ──────────────────────────
    t = time.perf_counter()
    bucket_issues = _bucketed_checksum(db, hash_cols, bucket_count=10)
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

    result.duration_ms = (time.perf_counter() - t0) * 1000
    return result


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _count_duplicates(db: SQLiteLoader, table: str, cols: list[str]) -> int:
    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = f"""
        SELECT COALESCE(SUM(cnt - 1), 0)
        FROM (
            SELECT COUNT(*) AS cnt
            FROM {table}
            GROUP BY {col_list}
            HAVING COUNT(*) > 1
        )
    """
    return db.scalar(sql) or 0


def _row_aligned_diff(
    db: SQLiteLoader,
    cols: list[str],
    pk_cols: list[str],
    limit: int = 20,
) -> pd.DataFrame:
    """
    Joins source and target on the primary key, finds rows where
    any non-PK column differs.
    """
    src_df = db.dataframe("source_df")[cols]
    tgt_df = db.dataframe("target_df")[cols]

    merged = src_df.merge(tgt_df, on=pk_cols, suffixes=("_src", "_tgt"))

    non_pk = [c for c in cols if c not in pk_cols]
    diff_mask = pd.Series(False, index=merged.index)
    for col in non_pk:
        src_col = f"{col}_src"
        tgt_col = f"{col}_tgt"
        if src_col in merged.columns and tgt_col in merged.columns:
            diff_mask |= merged[src_col].astype(str) != merged[tgt_col].astype(str)

    return merged[diff_mask].head(limit)


def _bucketed_checksum(
    db: SQLiteLoader,
    cols: list[str],
    bucket_count: int = 10,
) -> list[dict]:
    """
    Python-side bucketed hash comparison.
    Splits rows into N buckets by (row_index % N).
    No PK needed.
    """
    src_df = db.dataframe("source_df")[cols].fillna("__NULL__").astype(str)
    tgt_df = db.dataframe("target_df")[cols].fillna("__NULL__").astype(str)

    # Sort both the same way for consistent bucketing
    src_sorted = src_df.sort_values(by=cols).reset_index(drop=True)
    tgt_sorted = tgt_df.sort_values(by=cols).reset_index(drop=True)

    mismatches = []
    for b in range(bucket_count):
        src_bucket = src_sorted[src_sorted.index % bucket_count == b]
        tgt_bucket = tgt_sorted[tgt_sorted.index % bucket_count == b]

        src_hash = hashlib.md5(src_bucket.to_csv(index=False).encode()).hexdigest()
        tgt_hash = hashlib.md5(tgt_bucket.to_csv(index=False).encode()).hexdigest()

        if src_hash != tgt_hash:
            mismatches.append({
                "bucket": b,
                "source_hash": src_hash,
                "target_hash": tgt_hash,
                "source_rows": len(src_bucket),
                "target_rows": len(tgt_bucket),
            })
    return mismatches