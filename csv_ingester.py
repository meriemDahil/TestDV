"""
pipeline/csv_ingester.py
-------------------------
Responsibility: load raw CSV files into already-created PostgreSQL tables.

Rules enforced here:
  - Only loads tables that have a matching schema definition
  - Column names are normalised to lowercase
  - Missing or extra CSV columns are rejected before any insert
  - Columns reordered to match the declared schema
  - Uses if_exists="append" safely because SchemaManager.drop_tables
    + create_tables always runs first, guaranteeing an empty table

Nothing here knows about SQL transformation or output tables.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger
from sqlalchemy import Engine


class CSVIngester:
    """
    Loads declared input CSV files into PostgreSQL.

    Parameters
    ----------
    engine:
        Injected SQLAlchemy Engine — never built internally.
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    def load(self, data_dir: Path, schemas: list) -> None:
        """
        For each TableSchema, find the matching CSV in data_dir,
        validate columns, and insert rows into the table.

        Parameters
        ----------
        data_dir:
            Directory that contains the source CSV files.
            Each file must be named <table_name>.csv.
        schemas:
            List of TableSchema objects — only files with a matching
            schema entry are loaded. Unknown CSVs are ignored.

        Raises
        ------
        FileNotFoundError
            If a schema-declared table has no matching CSV.
        ValueError
            If the CSV has missing or extra columns vs the schema.
        """
        logger.info("Loading CSV input tables ...")

        for schema in schemas:
            csv_path = data_dir / f"{schema.table_name}.csv"
            self._load_one(csv_path, schema)

    # ── Internal ─────────────────────────────────────────────────

    def _load_one(self, csv_path: Path, schema) -> None:
        """Load a single CSV file into its PostgreSQL table."""

        if not csv_path.exists():
            raise FileNotFoundError(
                f"No CSV found for table '{schema.table_name}'. "
                f"Expected: {csv_path}"
            )

        # Read as strings — avoids pandas coercing 30 → 30.0, dates, etc.
        # Type enforcement is PostgreSQL's job, not pandas'.
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)

        # Normalize headers: strip whitespace + lowercase
        df.columns = [c.strip().lower() for c in df.columns]

        expected = [col.name for col in schema.columns]
        self._validate_columns(df.columns.tolist(), expected, csv_path.name)

        # Reorder to match schema — defensive against CSV column ordering
        df = df[expected]

        # if_exists="append" is safe here because drop_tables + create_tables
        # always runs before this, so the table is guaranteed to be empty.
        df.to_sql(
            schema.table_name,
            self.engine,
            if_exists="append",
            index=False,
            method="multi",
        )

        logger.success(
            f"  {csv_path.name:<40} → '{schema.table_name}' ({len(df):,} rows)"
        )

    @staticmethod
    def _validate_columns(
        actual:   list[str],
        expected: list[str],
        filename: str,
    ) -> None:
        """
        Raise a descriptive error if the CSV columns don't match the schema.
        Catches two common problems:
          - A column was renamed or dropped in the CSV
          - An extra column was added to the CSV without updating the schema
        """
        actual_set   = set(actual)
        expected_set = set(expected)

        missing = sorted(expected_set - actual_set)
        extra   = sorted(actual_set   - expected_set)

        errors: list[str] = []
        if missing:
            errors.append(f"missing columns required by schema: {missing}")
        if extra:
            errors.append(f"unexpected columns not in schema: {extra}")

        if errors:
            raise ValueError(
                f"CSV '{filename}' column mismatch — " + " | ".join(errors)
            )
