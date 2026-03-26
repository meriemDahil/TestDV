"""
pipeline/sql_runner.py
-----------------------
Responsibility: everything that touches the SQL SCRIPT FILE.

  - Split SQL into individual statements (sqlglot-powered parser)
  - Filter comment-only statements
  - Validate SQL via layer0 before execution
  - Execute statements sequentially in a single transaction
  - Detect INSERT INTO targets and return their contents as DataFrames
  - Save validation log + error log on failure

Nothing here knows about schemas, CSVs, or output file paths.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
import sqlglot
from sqlglot import exp
from loguru import logger
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError


# ── sqlglot dialect used throughout ──────────────────────────────────────────
# Centralised so a future migration (e.g. to Redshift) only touches one line.
_DIALECT: sqlglot.Dialect = "postgres"


class SQLRunner:
    """
    Validates and executes a SQL transformation script against PostgreSQL.

    Parameters
    ----------
    engine:
        Injected SQLAlchemy Engine.
    sql_validator:
        Callable(sql_script: str) -> LayerResult.
        Defaults to layer0_validate_sql.run.
        Injectable for testing without importing the real validator.
    validation_log_path:
        Where to write the Layer 0 validation JSON log.
    error_log_path:
        Where to write SQL execution error details.
    """

    def __init__(
        self,
        engine:              Engine,
        sql_validator=       None,
        validation_log_path: Path | None = None,
        error_log_path:      Path | None = None,
    ):
        self.engine              = engine
        self.validation_log_path = validation_log_path
        self.error_log_path      = error_log_path

        if sql_validator is None:
            from utils.comparaison import layer0_validate_sql
            self._validator = layer0_validate_sql.run
        else:
            self._validator = sql_validator

    # ── Public API ────────────────────────────────────────────────

    def run(self, sql_path: Path) -> dict[str, pd.DataFrame]:
        """
        Full pipeline for one SQL file:
          1. Read file
          2. Validate (layer0)
          3. Split into statements
          4. Execute sequentially
          5. Return dict[table_name -> DataFrame] for every INSERT target

        Raises
        ------
        RuntimeError
            If validation fails or no executable statements found.
        SQLAlchemyError
            On database execution failure (error log written first).
        """
        logger.info(f"Running SQL from {sql_path} ...")
        sql_script = sql_path.read_text(encoding="utf-8")

        self._validate(sql_script)

        statements = self.split_statements(sql_script)
        exec_stmts = [s for s in statements if not self._is_comment_only(s)]

        if not exec_stmts:
            raise RuntimeError("No executable SQL statements found in script.")

        return self._execute(exec_stmts)

    def split_statements(self, sql: str) -> list[str]:
        """
        Split a SQL script into individual statements using sqlglot.

        sqlglot's parser natively handles everything the previous
        hand-rolled char-by-char loop covered -- plus more:

          - Semicolons inside single-quoted strings  ('it''s fine')
          - Semicolons inside double-quoted identifiers ("col;name")
          - Line comments  (-- comment)
          - Block comments (/* comment */)
          - PostgreSQL dollar-quoted strings ($$body$$, $tag$body$tag$)
          - E-strings  (E'escape\\nsequence')

        Each non-None AST node is emitted back to SQL via
        ``node.sql(dialect=_DIALECT)`` so the returned strings are
        syntactically normalised and dialect-correct.  ``None`` entries
        produced by sqlglot for comment-only or empty input segments are
        filtered out here (``_is_comment_only`` provides the second-pass
        guard used by ``run()``).
        """
        if not sql or not sql.strip():
            return []

        ast_statements = sqlglot.parse(
            sql,
            dialect=_DIALECT,
            error_level=sqlglot.ErrorLevel.WARN,   # surface warnings; don't hard-crash
        )

        return [
            node.sql(dialect=_DIALECT)
            for node in ast_statements
            if node is not None                     # None == comment-only / empty segment
        ]

    # ── Internal helpers ──────────────────────────────────────────

    def _validate(self, sql_script: str) -> None:
        """Run layer0 validation. Raises RuntimeError on failure."""
        result = self._validator(sql_script)
        self._save_validation_log(result)

        if result.status.name == "FAILED":
            raise RuntimeError(
                "SQL validation failed -- check validation_log.json for details."
            )

        logger.success("SQL validation passed.")

    def _execute(self, statements: list[str]) -> dict[str, pd.DataFrame]:
        """
        Execute all statements in a single transaction.
        After each INSERT, snapshot the target table as a DataFrame.
        Returns a dict of all inserted tables.
        """
        tables: dict[str, pd.DataFrame] = {}

        with self.engine.begin() as conn:
            for stmt in statements:
                logger.debug(f"Executing:\n{stmt[:120]}{'...' if len(stmt) > 120 else ''}")
                try:
                    conn.execute(text(stmt))
                except SQLAlchemyError as e:
                    self._save_error_log(e, stmt)
                    raise

                table = self._extract_insert_target(stmt)
                if table:
                    df = pd.read_sql_query(
                        text(f'SELECT * FROM "{table}"'), conn
                    )
                    tables[table] = df

        logger.success(
            f"Execution complete -- {len(tables)} output table(s): "
            f"{list(tables.keys())}"
        )
        return tables

    @staticmethod
    def _is_comment_only(stmt: str) -> bool:
        """
        Return True if the statement contains no executable SQL --
        i.e. only line comments (--), block comments (/* */), or whitespace.

        Uses sqlglot.parse: a comment-only or empty input produces
        ``[None]``, whereas any real SQL node is non-None.

        Static method -- pure function, no side effects, easy to test.
        """
        if not stmt or not stmt.strip():
            return True

        parsed = sqlglot.parse(
            stmt,
            dialect=_DIALECT,
            error_level=sqlglot.ErrorLevel.WARN,
        )
        return all(node is None for node in parsed)

    @staticmethod
    def _extract_insert_target(stmt: str) -> str | None:
        """
        Return the table name from an INSERT INTO statement, or None.

        Uses the sqlglot AST instead of a hand-written regex so that
        quoted, schema-qualified, and mixed-case identifiers are all
        handled correctly.

        Examples
        --------
        'INSERT INTO public."Orders" ...'  ->  'Orders'
        'INSERT INTO staging VALUES ...'   ->  'staging'
        'SELECT 1'                         ->  None
        """
        try:
            node = sqlglot.parse_one(stmt, dialect=_DIALECT)
        except sqlglot.errors.SqlglotError:
            return None

        if not isinstance(node, exp.Insert):
            return None

        table = node.find(exp.Table)
        return table.name if table else None

    # ── Log writers ───────────────────────────────────────────────

    def _save_validation_log(self, result) -> None:
        if not self.validation_log_path:
            return

        self.validation_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "layer":       result.layer_name,
            "status":      result.status.name,
            "duration_ms": result.duration_ms,
            "checks": [
                {
                    "check_name": c.check_name,
                    "status":     c.status.name,
                    "message":    c.message,
                    "details":    c.details,
                    "duration_ms":c.duration_ms,
                }
                for c in result.checks
            ],
        }
        self.validation_log_path.write_text(
            json.dumps(payload, indent=4), encoding="utf-8"
        )
        logger.success(f"Validation log -> {self.validation_log_path}")

    def _save_error_log(self, e: Exception, stmt: str | None = None) -> None:
        if not self.error_log_path:
            logger.error(f"SQL execution error: {e}")
            return

        self.error_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp":     datetime.utcnow().isoformat(),
            "error_type":    type(e).__name__,
            "error_message": str(e),
            "db_error":      str(getattr(e, "orig", e)),
            "sql_statement": stmt,
            "traceback":     traceback.format_exc(),
        }
        self.error_log_path.write_text(
            json.dumps(payload, indent=4), encoding="utf-8"
        )
        logger.error(f"Error log -> {self.error_log_path}")
