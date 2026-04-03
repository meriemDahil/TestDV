"""
pipeline/inferred_csv_loader.py
--------------------------------
Replaces SchemaManager + CSVIngester when no DDL schema files are available.

Scans a directory for CSV files, infers column names directly from headers,
and loads each file into the database as a table named after the file stem.

Design decisions
----------------
- All columns are loaded as TEXT — type coercion stays in PostgreSQL,
  not pandas. This matches the original CSVIngester philosophy.
- Column names are normalised to lowercase and stripped of whitespace.
- Tables are always replaced (if_exists="replace") so every run starts
  from a clean slate with no prior DROP required.
- Output tables created by the SQL script are dropped before input CSVs
  are loaded, preventing stale rows from a previous run leaking in.
  Table detection is delegated to SQLRunner.created_tables() so the
  regex logic lives in exactly one place.
- Table names are validated against a safe-identifier pattern before
  being used in SQL — prevents injection through malicious filenames.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from loguru import logger
from sqlalchemy import Engine, text


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class InferredCSVLoader:
    """
    Scans a directory for CSV files and loads each one into a database
    table whose structure is inferred directly from the CSV headers.

    Parameters
    ----------
    engine:
        Injected SQLAlchemy Engine — never built internally.
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    # ── Public API ────────────────────────────────────────────────

    def load(self, data_dir: Path, sql_path: Path | None = None) -> list[str]:
        """
        Load every *.csv in data_dir into the database.

        Steps:
          1. Drop output tables detected in sql_path (via SQLRunner)
             so stale rows from previous runs cannot survive.
          2. For each CSV, derive the table name from the filename,
             normalise headers, and write to DB with if_exists="replace".

        Parameters
        ----------
        data_dir:
            Directory containing input CSV files.
        sql_path:
            Path to the SQL transformation script. When provided, tables
            named in CREATE TABLE statements are dropped before loading.

        Returns
        -------
        list[str]
            Names of the tables that were loaded, in discovery order.

        Raises
        ------
        ValueError
            If a CSV filename produces an unsafe SQL identifier.
        """
        data_dir  = Path(data_dir)
        csv_files = sorted(data_dir.glob("*.csv"))

        if not csv_files:
            logger.warning(f"No CSV files found in {data_dir}")
            return []

        logger.info(
            f"InferredCSVLoader: {len(csv_files)} CSV file(s) found in {data_dir}"
        )

        if sql_path:
            self._drop_output_tables(sql_path)

        loaded: list[str] = []
        for csv_path in csv_files:
            table_name = self._table_name_from(csv_path)
            self._load_one(csv_path, table_name)
            loaded.append(table_name)

        return loaded

    # ── Internal ─────────────────────────────────────────────────

    def _drop_output_tables(self, sql_path: Path) -> None:
        """
        Drop tables that the SQL script will recreate, so stale output
        rows from a previous run cannot survive into the next execution.

        Delegates table-name detection to SQLRunner.created_tables()
        so the parsing logic lives in exactly one place.
        """
        # Late import avoids a circular dependency
        # (SQLRunner imports nothing from this module).
        from pipeline.sql_runner import SQLRunner

        tables = SQLRunner.created_tables_from_path(sql_path)
        if not tables:
            return

        with self.engine.begin() as conn:
            for table in sorted(tables):
                conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
                logger.debug(f"  dropped output table: {table}")

    def _table_name_from(self, csv_path: Path) -> str:
        """
        Derive and validate a SQL table name from a CSV filename stem.

        'orders.csv'     → 'orders'       ✓
        'my-orders.csv'  → ValueError     (hyphen not allowed)
        '1table.csv'     → ValueError     (starts with digit)
        """
        name = csv_path.stem
        if not _SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError(
                f"CSV filename '{csv_path.name}' produces an unsafe table "
                f"name '{name}'. Rename the file using only letters, digits, "
                f"and underscores, starting with a letter or underscore."
            )
        return name

    def _load_one(self, csv_path: Path, table_name: str) -> None:
        """
        Read one CSV and write it to the database.

        - dtype=str keeps all values as TEXT — PostgreSQL handles casting.
        - keep_default_na=False prevents pandas from silently converting
          empty strings to NaN.
        - if_exists="replace" guarantees a clean table on every run.
        """
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        df.columns = [c.strip().lower() for c in df.columns]

        df.to_sql(
            table_name,
            self.engine,
            if_exists="replace",
            index=False,
            method="multi",
        )

        logger.success(
            f"  {csv_path.name:<40} → '{table_name}' "
            f"({len(df):,} rows, {len(df.columns)} columns)"
        )
