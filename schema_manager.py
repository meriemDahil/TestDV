"""
pipeline/schema_manager.py
---------------------------
Responsibility: everything that touches the DATABASE SCHEMA.

  - Load TableSchema objects from ddl/*.csv
  - Drop existing tables (input + SQL-created output tables)
  - Create tables from generated DDL

Nothing here reads or writes data rows.
Nothing here knows about CSV files or SQL transformation logic.
"""
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger
from sqlalchemy import Engine, text


# ─────────────────────────────────────────────
# Identifier safety
# ─────────────────────────────────────────────

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote(identifier: str) -> str:
    """
    Double-quote a PostgreSQL identifier after validating it.
    Raises ValueError on unsafe input — prevents SQL injection
    through table/column names.
    """
    if not _SAFE_IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Unsafe SQL identifier: {identifier!r}")
    return f'"{identifier}"'


# ─────────────────────────────────────────────
# SchemaManager
# ─────────────────────────────────────────────

class SchemaManager:
    """
    Manages PostgreSQL table lifecycle: load → drop → create.

    Parameters
    ----------
    engine:
        A SQLAlchemy Engine. Injected — never built internally.
        Allows tests to pass a SQLite engine with no real database.
    schema_loader:
        Callable that accepts a Path and returns list[TableSchema].
        Defaults to SchemaCSVLoader.load_all_from_dir.
        Injectable for testing without touching the filesystem.
    """

    def __init__(self, engine: Engine, schema_loader=None):
        self.engine = engine

        if schema_loader is None:
            # Late import — keeps this module usable in tests
            # that mock the loader without importing the real one.
            from utils.helpers.schema import SchemaCSVLoader
            self._loader = SchemaCSVLoader.load_all_from_dir
        else:
            self._loader = schema_loader

    # ── Public API ────────────────────────────────────────────────

    def load_schemas(self, ddl_dir: Path) -> list:
        """
        Read every *.csv in ddl_dir and return a list of TableSchema objects.
        One file = one table.
        """
        logger.info(f"Loading table schemas from {ddl_dir}")
        schemas = self._loader(ddl_dir)

        for s in schemas:
            pk = f"  pk={s.primary_key_columns}" if s.primary_key_columns else ""
            logger.success(f"  {s.table_name:<24} {len(s.columns)} columns{pk}")

        return schemas

    def drop_tables(
        self,
        schemas:  list,
        sql_path: Path | None = None,
    ) -> None:
        """
        DROP IF EXISTS all tables that this pipeline owns:
          1. Input tables declared in schema CSVs
          2. Output tables created by the SQL script (detected via regex)

        Using CASCADE so foreign-key dependencies don't block the drop.
        """
        logger.info("Dropping tables for clean-state rebuild ...")

        tables_to_drop: set[str] = {s.table_name for s in schemas}

        # Also detect tables the SQL script creates — avoids stale data
        # from previous runs accumulating in INSERT-target tables.
        if sql_path and sql_path.exists():
            sql = sql_path.read_text(encoding="utf-8")
            found = re.findall(
                r'CREATE\s+(?:TEMP\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                r'(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?'
                r'"?([A-Za-z_][A-Za-z0-9_]*)"?',
                sql,
                re.IGNORECASE,
            )
            for _, table in found:
                tables_to_drop.add(table)

        with self.engine.begin() as conn:
            for table in sorted(tables_to_drop):
                conn.execute(text(f"DROP TABLE IF EXISTS {_quote(table)} CASCADE"))
                logger.debug(f"  dropped: {table}")

        logger.success(f"Dropped {len(tables_to_drop)} table(s).")

    def create_tables(self, schemas: list) -> None:
        """
        Execute the generated CREATE TABLE DDL for every schema.
        Tables must have been dropped first (or IF NOT EXISTS is used).
        """
        logger.info("Creating PostgreSQL tables ...")

        with self.engine.begin() as conn:
            for schema in schemas:
                ddl = schema.to_ddl()
                logger.debug(f"DDL [{schema.table_name}]:\n{ddl}")
                conn.execute(text(ddl))

        logger.success(f"Created {len(schemas)} table(s): "
                       f"{[s.table_name for s in schemas]}")
