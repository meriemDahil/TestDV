"""
Validation Engine – Data Loader

CSV  →  pandas DataFrame  →  normalize  →  done.

No SQLite. No in-memory database.
The DataFrames ARE the working dataset for all validation layers.
All comparisons happen directly on src_df and tgt_df.
"""
from __future__ import annotations

import chardet
import pandas as pd
import unidecode


# Normalization chain

class ColumnNormalizer:
    """
    Standardizes column names:
      'First Name ' → 'first_name'
      'Montant-HT'  → 'montant_ht'
    """
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

        # Resolve duplicate column names that appear after normalization
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
    """
    Unifies all null-like representations to actual None / NaN.
    Covers the most common variants found in exported CSVs.
    """
    NULL_VALUES = {
        "", " ", "null", "none", "na", "n/a",
        "?", "nan", "nil", "n.a.", "nd", "#n/a",
    }

    @classmethod
    def _norm(cls, v):
        if v is None:
            return None
        if str(v).strip().lower() in cls.NULL_VALUES:
            return None
        return v

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.applymap(self._norm)


class StringTrimNormalizer:
    """Strips leading/trailing whitespace from all string columns."""
    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in df.select_dtypes(include="object").columns:
            df[col] = df[col].str.strip()
        return df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies the full normalization chain in order.
    Deterministic — no randomness, same output for same input.
    """
    df = ColumnNormalizer().apply(df)
    df = NullNormalizer().apply(df)
    df = StringTrimNormalizer().apply(df)
    return df


# CSV Reader

class CSVReader:
    """
    Reads a CSV file with automatic encoding and delimiter detection.
    Handles UTF-8, latin-1, ISO-8859-1, and common European formats.
    """
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

    def read(self) -> pd.DataFrame:
        encoding = self.detect_encoding()
        sep = self.detect_separator(encoding)
        return pd.read_csv(
            self.path,
            encoding=encoding,
            sep=sep,
            low_memory=False,
            keep_default_na=False,  # we handle nulls ourselves
        )


def load_csv(path: str) -> pd.DataFrame:
    """
    Single entry point: read CSV → normalize → return DataFrame.
    This is all that happens. No database, no staging table.
    """
    raw = CSVReader(path).read()
    return normalize(raw)