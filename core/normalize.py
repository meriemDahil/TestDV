"""
Validation Engine – Data Loader (SQLite backend)

Loads both DataFrames into an in-memory SQLite database.
SQLite ships with Python — zero extra dependencies.

Limitations acknowledged:
  - No SHA256 built-in → hashing done in Python
  - No QUANTILE_CONT → percentiles computed in Python via pandas
  - STDDEV not built-in → computed in Python
These are handled explicitly below.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

import chardet
import pandas as pd
import unidecode


# ──────────────────────────────────────────────
# Normalization (your original pipeline, fixed)
# ──────────────────────────────────────────────

class ColumnNormalizer:
    @staticmethod
    def normalize(name: str) -> str:
        return (
            unidecode.unidecode(name)
            .strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
        )

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        normalized = [self.normalize(c) for c in df.columns]

        # Resolve duplicates that appear after normalization
        seen: dict[str, int] = {}
        deduped = []
        for col in normalized:
            if col in seen:
                seen[col] += 1
                deduped.append(f"{col}_{seen[col]}")
            else:
                seen[col] = 0
                deduped.append(col)

        df.columns = deduped
        return df


class NullNormalizer:
    NULL_VALUES = {"", " ", "null", "none", "na", "n/a", "?", "nan", "nil", "n.a.", "nd"}

    @classmethod
    def _norm(cls, v):
        if v is None:
            return None
        if str(v).strip().lower() in cls.NULL_VALUES:
            return None
        return v

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        # applymap for element-wise, not apply (which is column-wise)
        return df.applymap(self._norm)


class StringTrimNormalizer:
    """Strip leading/trailing whitespace from all string columns."""
    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.strip()
        return df


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Full normalization chain — deterministic, no randomness."""
    df = ColumnNormalizer().apply(df)
    df = NullNormalizer().apply(df)
    df = StringTrimNormalizer().apply(df)
    return df


# ──────────────────────────────────────────────
# CSV Reader
# ──────────────────────────────────────────────

class CSVReader:
    SEPARATORS = [",", ";", "\t", "|"]

    def __init__(self, path: str):
        self.path = path

    def detect_encoding(self) -> str:
        with open(self.path, "rb") as f:
            raw = f.read(50_000)
        return chardet.detect(raw)["encoding"] or "utf-8"

    def detect_separator(self, encoding: str) -> str:
        with open(self.path, "r", encoding=encoding, errors="replace") as f:
            sample = f.read(4096)
        counts = {sep: sample.count(sep) for sep in self.SEPARATORS}
        return max(counts, key=counts.get)

    def load(self) -> pd.DataFrame:
        encoding = self.detect_encoding()
        sep = self.detect_separator(encoding)
        return pd.read_csv(
            self.path,
            encoding=encoding,
            sep=sep,
            low_memory=False,
            keep_default_na=False,   # we handle nulls ourselves
        )


def load_csv(path: str) -> pd.DataFrame:
    return normalize_dataframe(CSVReader(path).load())


# ──────────────────────────────────────────────
# SQLite Loader
# ──────────────────────────────────────────────

SOURCE_TABLE = "source_df"
TARGET_TABLE = "target_df"


class SQLiteLoader:
    """
    Loads both DataFrames into an in-memory SQLite connection.
    All validation layers query through this single connection.

    SQLite limitations handled explicitly:
      - No SHA256 / hash() → Python-side hashing
      - No STDDEV / QUANTILE → pandas fallback
      - No BOOLEAN type → stored as INTEGER (0/1)
    """

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._source: pd.DataFrame | None = None
        self._target: pd.DataFrame | None = None

    def load_both(self, source: pd.DataFrame, target: pd.DataFrame) -> None:
        self._source = source
        self._target = target
        source.to_sql(SOURCE_TABLE, self.conn, if_exists="replace", index=False)
        target.to_sql(TARGET_TABLE, self.conn, if_exists="replace", index=False)

    # ── Query helpers ────────────────────────────────────────────────

    def query(self, sql: str) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn)

    def scalar(self, sql: str) -> Any:
        cur = self.conn.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None

    def get_columns(self, table: str) -> list[str]:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        return [r["name"] for r in cur.fetchall()]

    def get_dtypes(self, table: str) -> dict[str, str]:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        return {r["name"]: r["type"].upper() for r in cur.fetchall()}

    def row_count(self, table: str) -> int:
        return self.scalar(f"SELECT COUNT(*) FROM {table}") or 0

    # ── Python-side helpers (SQLite doesn't have these built-ins) ─────

    def dataframe(self, table: str) -> pd.DataFrame:
        """Returns the original DataFrame (faster than re-querying)."""
        return self._source if table == SOURCE_TABLE else self._target

    def column_hash(self, table: str, col: str) -> str:
        """Deterministic hash of one column (sorted, null-safe)."""
        df = self.dataframe(table)
        series = df[col].fillna("__NULL__").astype(str).sort_values()
        content = "\n".join(series).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def dataset_hash(self, table: str, cols: list[str]) -> str:
        """
        Order-independent hash of the full dataset.
        Rows are sorted by all columns before hashing — safe against
        engines returning rows in different sequences.
        """
        df = self.dataframe(table)[sorted(cols)].copy()
        df = df.fillna("__NULL__").astype(str)
        df_sorted = df.sort_values(by=list(df.columns)).reset_index(drop=True)
        content = df_sorted.to_csv(index=False).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def stddev(self, table: str, col: str) -> float | None:
        """Python fallback for STDDEV (not built into SQLite)."""
        try:
            return float(self.dataframe(table)[col].dropna().std())
        except Exception:
            return None

    def percentile(self, table: str, col: str, p: float) -> float | None:
        """Python fallback for QUANTILE (not built into SQLite)."""
        try:
            return float(self.dataframe(table)[col].dropna().quantile(p))
        except Exception:
            return None