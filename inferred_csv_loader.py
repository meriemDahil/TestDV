"""
pipeline/inferred_csv_loader.py
--------------------------------
Drop-in replacement for SchemaManager + CSVIngester when no DDL schema
files are available.

Instead of requiring a pre-declared schema for every input table, this
class scans a directory for CSV files, infers column names and types
directly from each file, and loads them into the database in one step.

Design decisions
----------------
- All columns are loaded as TEXT (same philosophy as CSVIngester) so
  PostgreSQL — not pandas — handles type coercion in the SQL script.
- Column names are normalised to lowercase and stripped of whitespace,
  matching the behaviour of the original CSVIngester.
- Tables are always fully replaced (if_exists="replace") so repeated
  runs start from a clean slate without needing a prior DROP.
- The table name is derived from the CSV filename without extension
  (e.g. "orders.csv" → table "orders"), exactly as before.
- Output tables created by the SQL script are dropped before loading
  so stale data from previous runs cannot accumulate. This mirrors
  SchemaManager.drop_tables(sql_path=...).

What is NOT done here
---------------------
- No column validation against a declared schema (there is none).
- No primary-key or constraint DDL (tables are plain TEXT columns).
- No SchemaManager or TableSchema objects are produced; the executor's
  ExecutionResult.schemas field will be an empty list when this loader
  is used.

Usage (standalone)
------------------
    from pipeline.inferred_csv_loader import InferredCSVLoader

    loader = InferredCSVLoader(engine)
    loader.load(data_dir=Path("data"), sql_path=Path("transform.sql"))

Usage (via SQLExecutor)
-----------------------
    executor = SQLExecutor.from_csv_only(
        engine   = engine,
        sql_path = Path("transform.sql"),
        data_dir = Path("data"),
    )
    result = executor.execute()
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from loguru import logger
from sqlalchemy import Engine, text


# Regex copied from schema_manager._SAFE_IDENTIFIER so table names derived
# from filenames are validated before being used in raw SQL.
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

        Steps performed:
          1. Drop output tables named in sql_path (if provided) so stale
             data from a previous run cannot leak into the new execution.
          2. For each CSV file found, derive the table name from the
             filename, normalise column headers, and upsert the table
             using if_exists="replace".

        Parameters
        ----------
        data_dir:
            Directory containing input CSV files.
        sql_path:
            Path to the SQL transformation script. When provided, any
            table named in a CREATE TABLE statement is dropped first.
            This mirrors SchemaManager.drop_tables(sql_path=sql_path).

        Returns
        -------
        list[str]
            Names of the tables that were loaded, in discovery order.

        Raises
        ------
        ValueError
            If a CSV filename produces an unsafe table name
            (e.g. "my-file.csv" → "my-file" is not a valid SQL identifier).
        """
        data_dir = Path(data_dir)
        csv_files = sorted(data_dir.glob("*.csv"))

        if not csv_files:
            logger.warning(f"No CSV files found in {data_dir}")
            return []

        logger.info(f"InferredCSVLoader: found {len(csv_files)} CSV file(s) in {data_dir}")

        # Drop SQL-created output tables before loading inputs so a fresh
        # run never reads rows from a previous execution.
        if sql_path:
            self._drop_output_tables(sql_path)

        loaded: list[str] = []
        for csv_path in csv_files:
            table_name = self._table_name_from(csv_path)
            self._load_one(csv_path, table_name)
            loaded.append(table_name)

        return loaded

    # ── Internal ─────────────────────────────────────────────────

    def _table_name_from(self, csv_path: Path) -> str:
        """
        Derive and validate a table name from a CSV filename.

        'orders.csv'        → 'orders'
        'My Orders.csv'     → ValueError  (space)
        'my-orders.csv'     → ValueError  (hyphen)
        """
        name = csv_path.stem  # filename without extension
        if not _SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError(
                f"CSV filename '{csv_path.name}' produces an unsafe table name "
                f"'{name}'. Rename the file to use only letters, digits, and "
                f"underscores, starting with a letter or underscore."
            )
        return name

    def _load_one(self, csv_path: Path, table_name: str) -> None:
        """
        Read a single CSV and write it to the database.

        - All columns are kept as TEXT (dtype=str) — type coercion is
          PostgreSQL's responsibility, not pandas'.
        - if_exists="replace" guarantees a clean table on every run
          without needing a prior explicit DROP.
        """
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)

        # Normalise headers: strip whitespace + lowercase
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

    def _drop_output_tables(self, sql_path: Path) -> None:
        """
        Drop tables that the SQL script creates (detected via regex on
        CREATE TABLE statements) so stale output rows from a previous
        run cannot survive into the next execution.

        Mirrors the sql_path branch of SchemaManager.drop_tables().
        """
        if not sql_path.exists():
            return

        sql = sql_path.read_text(encoding="utf-8")
        found = re.findall(
            r'CREATE\s+(?:TEMP\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            r'(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?'
            r'"?([A-Za-z_][A-Za-z0-9_]*)"?',
            sql,
            re.IGNORECASE,
        )

        if not found:
            return

        with self.engine.begin() as conn:
            for _, table in found:
                conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
                logger.debug(f"  dropped output table: {table}")
