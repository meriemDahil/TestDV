"""
pipeline/executor.py
---------------------
SQLExecutor — the thin orchestrator.

Wires InferredCSVLoader + SQLRunner + OutputManager into a single
pipeline. No DDL schema files are required — table structure is
inferred directly from CSV headers.

Every dependency is injected so the executor is fully testable:
  - Pass a SQLite engine  → no PostgreSQL needed
  - Pass a mock validator → no real SQL checking
  - Pass a temp output_dir → no real files written

Usage
-----
from pipeline.models   import make_postgres_engine
from pipeline.executor import SQLExecutor

engine = make_postgres_engine(user="...", password="...")

executor = SQLExecutor.from_paths(
    engine   = engine,
    sql_path = Path("sql_scripts/transformations.sql"),
    data_dir = Path("data"),
)

result = executor.execute()
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger
from sqlalchemy import Engine

from pipeline.models              import ExecutionResult
from pipeline.sql_runner          import SQLRunner
from pipeline.output_manager      import OutputManager
from pipeline.inferred_csv_loader import InferredCSVLoader


class SQLExecutor:
    """
    Orchestrates the SQL transformation pipeline without DDL schema files.

    Parameters
    ----------
    inferred_loader: InferredCSVLoader — scans CSVs, drops stale output
                                         tables, loads inputs into DB
    sql_runner:      SQLRunner         — validates + executes SQL script
    output_manager:  OutputManager     — saves CSVs + persists to DB
    sql_path:        Path              — the transformation SQL file
    data_dir:        Path              — directory containing input CSVs
    """

    def __init__(
        self,
        inferred_loader: InferredCSVLoader,
        sql_runner:      SQLRunner,
        output_manager:  OutputManager,
        sql_path:        Path,
        data_dir:        Path,
    ):
        self._inferred_loader = inferred_loader
        self._sql_runner      = sql_runner
        self._output_manager  = output_manager
        self.sql_path         = Path(sql_path)
        self.data_dir         = Path(data_dir)

    # ── Factory ───────────────────────────────────────────────────

    @classmethod
    def from_paths(
        cls,
        engine:            Engine,
        sql_path:          Path,
        data_dir:          Path,
        output_dir:        Path | None = None,
        issue_report_path: Path | None = None,
        sql_validator=     None,
    ) -> "SQLExecutor":
        """
        Build a fully wired SQLExecutor from paths and an engine.
        No DDL schema files required.

        Parameters
        ----------
        engine:
            SQLAlchemy Engine (PostgreSQL or SQLite for tests).
        sql_path:
            Path to the SQL transformation script.
        data_dir:
            Directory containing input CSV files.
        output_dir:
            Where to write output CSVs. Defaults to data_dir.
        issue_report_path:
            Where to write the validation/error JSON report.
            Defaults to data_dir/validation_report.json.
        sql_validator:
            Optional override for the SQL validator callable.
        """
        data_dir   = Path(data_dir)
        output_dir = Path(output_dir) if output_dir else data_dir

        return cls(
            inferred_loader = InferredCSVLoader(engine),
            sql_runner      = SQLRunner(
                engine            = engine,
                sql_validator     = sql_validator,
                issue_report_path = issue_report_path or data_dir / "validation_report.json",
            ),
            output_manager  = OutputManager(engine, output_dir=output_dir),
            sql_path        = sql_path,
            data_dir        = data_dir,
        )

    # ── Main entry point ──────────────────────────────────────────

    def execute(
        self,
        persist_to_db: bool = True,
        preview:       bool = True,
    ) -> ExecutionResult:
        """
        Run the full pipeline end to end:

          1. Drop stale output tables (tables created by the SQL script)
          2. Load all CSVs from data_dir into the database
          3. Validate + execute the SQL transformation script
          4. Preview results (optional)
          5. Save output CSVs + persist to DB (optional)

        Parameters
        ----------
        persist_to_db:
            Write output DataFrames back to PostgreSQL. Default True.
        preview:
            Print a console preview of output tables. Default True.

        Returns
        -------
        ExecutionResult
            Contains an empty schemas list and all output DataFrames.
        """
        _banner("PostgreSQL SQL EXECUTOR")

        # ── Steps 1-2: prepare the database ───────────────────────
        self._inferred_loader.load(self.data_dir, sql_path=self.sql_path)

        # ── Step 3: validate + execute the SQL ────────────────────
        output_tables = self._sql_runner.run(self.sql_path)

        # ── Steps 4-5: present and persist results ─────────────────
        if preview:
            self._output_manager.preview(output_tables)

        self._output_manager.save_all(
            output_tables,
            persist_to_db=persist_to_db,
        )

        _banner("DONE")

        return ExecutionResult(schemas=[], output_tables=output_tables)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _banner(text: str) -> None:
    logger.info("=" * 70)
    logger.info(f"  {text}")
    logger.info("=" * 70)
