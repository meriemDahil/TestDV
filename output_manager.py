"""
pipeline/output_manager.py
---------------------------
Responsibility: persist and display execution results.

  - Save DataFrames to CSV files
  - Persist DataFrames to PostgreSQL
  - Console preview

Nothing here knows about schemas, SQL scripts, or validation.
Completely stateless — every method is independent.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger
from sqlalchemy import Engine


class OutputManager:
    """
    Handles all output operations after the SQL transformation completes.

    Parameters
    ----------
    engine:
        Injected SQLAlchemy Engine for database persistence.
    output_dir:
        Directory where CSV files are written.
        Created automatically if it doesn't exist.
    """

    def __init__(self, engine: Engine, output_dir: Path):
        self.engine     = engine
        self.output_dir = output_dir

    # ── Public API ────────────────────────────────────────────────

    def save_all(
        self,
        tables:           dict[str, pd.DataFrame],
        persist_to_db:    bool = True,
        extra_csv_paths:  dict[str, Path] | None = None,
    ) -> None:
        """
        Save every output table to CSV and optionally to PostgreSQL.

        Parameters
        ----------
        tables:
            Dict of table_name → DataFrame from run_transformation.
        persist_to_db:
            If True, each DataFrame is also written to PostgreSQL
            with if_exists="replace" — clean overwrite every run.
        extra_csv_paths:
            Optional additional CSV destinations per table.
            e.g. {"out1": Path("data/testbench_output.csv")}
            Used for backward-compatible dual-save (testbench + sql_generated).
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)

        for table_name, df in tables.items():
            self._save_csv(df, table_name)

            if extra_csv_paths and table_name in extra_csv_paths:
                path = extra_csv_paths[table_name]
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(path, index=False)
                logger.success(f"  extra CSV → {path}")

            if persist_to_db:
                self._persist(df, table_name)

    def preview(self, tables: dict[str, pd.DataFrame], max_rows: int = 50) -> None:
        """Print a console preview of every output table."""
        for table_name, df in tables.items():
            logger.info(f"Preview: '{table_name}' ({len(df):,} rows)")
            print(df.head(max_rows).to_string(index=False))
            if len(df) > max_rows:
                print(f"  ... ({len(df) - max_rows} more rows)")

    def save_csv(self, df: pd.DataFrame, filename: str) -> Path:
        """
        Save a single DataFrame to a named CSV in output_dir.
        Returns the path written.
        """
        return self._save_csv(df, filename)

    def persist(self, df: pd.DataFrame, table_name: str) -> None:
        """Persist a single DataFrame to PostgreSQL."""
        self._persist(df, table_name)

    # ── Internal ─────────────────────────────────────────────────

    def _save_csv(self, df: pd.DataFrame, name: str) -> Path:
        path = self.output_dir / f"{name}.csv"
        df.to_csv(path, index=False)
        logger.success(f"  CSV saved  → {path}  ({len(df):,} rows)")
        return path

    def _persist(self, df: pd.DataFrame, table_name: str) -> None:
        """
        Write DataFrame to PostgreSQL with if_exists="replace".
        This is a full overwrite — no duplicate rows accumulate.

        Note: to_sql with replace drops and recreates the table using
        pandas-inferred types, which loses DDL constraints. This is
        intentional for output/reporting tables. Source tables are
        always managed via SchemaManager DDL.
        """
        df.to_sql(
            table_name,
            self.engine,
            if_exists="replace",
            index=False,
        )
        logger.success(f"  DB persisted → '{table_name}'  ({len(df):,} rows)")
