from __future__ import annotations

import chardet
import pandas as pd
import unidecode


# ============================================================
# COLUMN NORMALIZATION
# ============================================================

class ColumnNormalizer:
    """
    Standardizes column names into predictable snake_case form.

    Examples:
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
        normalized = [self.normalize(str(c)) for c in df.columns]

        # Deduplicate names created after normalization
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


# ============================================================
# NULL NORMALIZATION
# ============================================================

class NullNormalizer:
    NULL_VALUES = {
        "", " ", "null", "Null", "NULL",
        "none", "None", "NONE",
        "na", "NA", "n/a", "N/A",
        "?", "nan", "NaN", "NAN",
        "nil", "n.a.", "nd", "#n/a", "#N/A"
    }

    @classmethod
    def _norm(cls, v):
        if v is None:
            return None

        text = str(v).strip().lower()
        return None if text in cls.NULL_VALUES else v

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Universal implementation — works on ALL pandas versions.
        Uses Series.map instead of DataFrame.applymap.
        """
        df = df.copy()
        for col in df.columns:
            df[col] = df[col].map(self._norm)
        return df


# ============================================================
# STRING TRIMMING
# ============================================================

class StringTrimNormalizer:
    """Trim whitespace from object/string columns."""

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in df.select_dtypes(include=["object", "string"]).columns:
            df[col] = df[col].astype(str).str.strip()
        return df


# ============================================================
# DATE NORMALIZATION (SAFE VERSION)
# ============================================================

class DateNormalizer:
    """
    Safely convert date-like columns into pandas datetime.

    Protects:
      • YEAR-only columns ("2020") → NOT treated as dates.
      • Prevents datetime → float issues during numeric inference.
    """

    NAME_HINTS = ("date", "time", "timestamp", "_at")
    MIN_SUCCESS_RATIO = 0.8
    MIN_SAMPLE = 20

    @classmethod
    def _has_date_hint(cls, col_name: str) -> bool:
        return any(token in col_name.lower() for token in cls.NAME_HINTS)

    @classmethod
    def _looks_like_full_date(cls, series: pd.Series) -> bool:
        non_null = series.dropna().astype(str)

        # Reject if >50% are pure 4-digit years
        year_like = non_null.str.fullmatch(r"\d{4}").sum()
        if year_like / len(non_null) > 0.5:
            return False

        return True

    @classmethod
    def _should_convert(cls, col_name: str, series: pd.Series) -> bool:
        if series.dtype.kind != "O":  # Only string/object
            return False

        non_null = series.dropna()
        if len(non_null) < cls.MIN_SAMPLE:
            return False

        if not cls._looks_like_full_date(series):
            return False

        if cls._has_date_hint(col_name):
            return True

        # Check success ratio
        parsed = pd.to_datetime(non_null, errors="coerce", dayfirst=True, format="mixed")
        success_ratio = parsed.notna().sum() / len(non_null)

        return success_ratio >= cls.MIN_SUCCESS_RATIO

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in df.columns:
            if self._should_convert(str(col), df[col]):
                df[col] = pd.to_datetime(
                    df[col],
                    errors="coerce",
                    dayfirst=True,
                    format="mixed",
                )

        return df


# ============================================================
# NORMALIZATION PIPELINE
# ============================================================

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = ColumnNormalizer().apply(df)
    df = NullNormalizer().apply(df)
    df = StringTrimNormalizer().apply(df)
    df = DateNormalizer().apply(df)
    return df


# ============================================================
# CSV READER (ENCODING + DELIMITER AUTODETECTION)
# ============================================================

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

    def read(self) -> pd.DataFrame:
        encoding = self.detect_encoding()
        sep = self.detect_separator(encoding)
        return pd.read_csv(
            self.path,
            encoding=encoding,
            sep=sep,
            low_memory=False,
            keep_default_na=False,
        )


# ============================================================
# DATE-LIKE COLUMN GUARD — centralised helper
# ============================================================

# FIX: Single source of truth for all name-based date/time hints.
# Previously these were scattered inline inside load_csv, causing
# inconsistent checks and allowing some date columns to leak through
# into the numeric inference block.
_DATE_HINTS = ("date", "time", "timestamp", "_at", "year")


def _is_date_like_column(col_name: str, series: pd.Series) -> bool:
    """
    Return True if this column should be treated as a date/temporal
    column and therefore SKIPPED by numeric inference.

    Checks (in order):
    1. Column already holds a parsed datetime dtype.
    2. Column name contains a known date-related hint.
    3. All non-null values look like 4-digit year strings (e.g. '2021').
    4. All non-null values look like float-encoded years (e.g. 2021.0
       where the fractional part is .0) — this is the key regression
       case: when a year column sneaks through as float64 it must still
       be recognised and skipped here so we don't re-cast it as a plain
       number.
    """
    # 1. Already a datetime
    if pd.api.types.is_datetime64_any_dtype(series):
        return True

    name = col_name.lower()

    # 2. Name hint
    if any(hint in name for hint in _DATE_HINTS):
        return True

    non_null = series.dropna()
    if non_null.empty:
        return False

    # 3. Year-like strings: '2021', '1999', …
    as_str = non_null.astype(str)
    if as_str.str.fullmatch(r"\d{4}").mean() > 0.5:
        return True

    # 4. FIX — Year-like floats that slipped through as float64: 2021.0
    #    This happens when DateNormalizer skipped a small dataset
    #    (MIN_SAMPLE not met) and numeric inference ran first.
    #    We detect them here so load_csv doesn't re-infer them as plain
    #    numbers, which would strip the temporal semantics entirely.
    if pd.api.types.is_float_dtype(series):
        # Check: integer part is in plausible year range AND fractional
        # part is always zero (i.e. these are really integers stored as float)
        numeric_vals = pd.to_numeric(non_null, errors="coerce").dropna()
        if not numeric_vals.empty:
            frac_is_zero = (numeric_vals % 1 == 0).all()
            in_year_range = numeric_vals.between(1000, 9999).mean() > 0.5
            if frac_is_zero and in_year_range:
                return True

    return False


# ============================================================
# HIGH-LEVEL CSV LOADER
# ============================================================

def load_csv(path: str) -> pd.DataFrame:
    """
    High-level CSV loader with safe normalization and numeric inference.

    Key fix: numeric inference now delegates the date/temporal guard to
    _is_date_like_column(), which handles all four cases — datetime
    dtype, name hints, year-string columns, and year-as-float columns —
    from a single, consistent place. The original code had duplicated
    datetime checks and an incomplete name-hint list, which allowed
    year values to be downcast to float64 before reaching the SQL layer.
    """
    df = pd.read_csv(path)
    df = normalize(df)

    for col in df.columns:

        # FIX: replaced the duplicated / inconsistent guards with a
        # single call to _is_date_like_column().
        if _is_date_like_column(str(col), df[col]):
            continue

        non_null_str = df[col].dropna().astype(str)

        # Skip columns that are entirely empty after normalization
        if non_null_str.empty:
            continue

        # Numeric inference
        numeric = pd.to_numeric(df[col], errors="coerce")
        if numeric.notna().sum() >= (df[col].notna().sum() * 0.5):
            df[col] = numeric

    return df
