"""
pipeline/executor.py
---------------------
SQLExecutor — the thin orchestrator.

Wires SchemaManager + CSVIngester + SQLRunner + OutputManager
into a single pipeline. Contains NO business logic of its own.

Every dependency is injected so the executor is fully testable:
  - Pass a SQLite engine → no PostgreSQL needed
  - Pass a mock validator → no real SQL checking
  - Pass a temp output_dir → no real files written

Usage
-----
from pipeline.models      import make_postgres_engine
from pipeline.executor    import SQLExecutor

engine = make_postgres_engine(user="...", password="...")

executor = SQLExecutor.from_paths(
    engine   = engine,
    sql_path = Path("sql_scripts/transformations.sql"),
    ddl_dir  = Path("ddl"),
    data_dir = Path("data"),
)

result = executor.execute()
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger
from sqlalchemy import Engine

from pipeline.models         import ExecutionResult
from pipeline.schema_manager import SchemaManager
from pipeline.csv_ingester   import CSVIngester
from pipeline.sql_runner     import SQLRunner
from pipeline.output_manager import OutputManager


class SQLExecutor:
    """
    Orchestrates the full SQL transformation pipeline.

    Parameters
    ----------
    schema_manager:  SchemaManager  — load / drop / create tables
    csv_ingester:    CSVIngester    — load CSV rows into tables
    sql_runner:      SQLRunner      — validate + execute SQL script
    output_manager:  OutputManager  — save CSVs + persist to DB
    sql_path:        Path           — the transformation SQL file
    ddl_dir:         Path           — schema CSV directory
    data_dir:        Path           — input CSV data directory
    """

    def __init__(
        self,
        schema_manager:  SchemaManager,
        csv_ingester:    CSVIngester,
        sql_runner:      SQLRunner,
        output_manager:  OutputManager,
        sql_path:        Path,
        ddl_dir:         Path,
        data_dir:        Path,
    ):
        self._schema_manager = schema_manager
        self._csv_ingester   = csv_ingester
        self._sql_runner     = sql_runner
        self._output_manager = output_manager
        self.sql_path        = Path(sql_path)
        self.ddl_dir         = Path(ddl_dir)
        self.data_dir        = Path(data_dir)

    # ── Factory — convenience constructor ─────────────────────────

    @classmethod
    def from_paths(
        cls,
        engine:              Engine,
        sql_path:            Path,
        ddl_dir:             Path,
        data_dir:            Path,
        output_dir:          Path | None  = None,
        validation_log_path: Path | None  = None,
        error_log_path:      Path | None  = None,
        sql_validator=       None,
        schema_loader=       None,
    ) -> "SQLExecutor":
        """
        Build a fully wired SQLExecutor from paths and an engine.
        All components are instantiated with sensible defaults.

        Parameters
        ----------
        engine:
            SQLAlchemy Engine (PostgreSQL or SQLite for tests).
        sql_path:
            Path to the SQL transformation script.
        ddl_dir:
            Directory containing schema CSVs (one per table).
        data_dir:
            Directory containing input CSV data files.
        output_dir:
            Where to write output CSVs. Defaults to data_dir.
        validation_log_path:
            Path for validation JSON log. Defaults to data/validation_log.json.
        error_log_path:
            Path for error JSON log. Defaults to data/error_log.json.
        sql_validator:
            Optional override for the SQL validator callable.
        schema_loader:
            Optional override for the schema loader callable.
        """
        data_dir   = Path(data_dir)
        output_dir = Path(output_dir) if output_dir else data_dir

        return cls(
            schema_manager  = SchemaManager(engine, schema_loader=schema_loader),
            csv_ingester    = CSVIngester(engine),
            sql_runner      = SQLRunner(
                engine              = engine,
                sql_validator       = sql_validator,
                validation_log_path = validation_log_path or data_dir / "validation_log.json",
                error_log_path      = error_log_path      or data_dir / "error_log.json",
            ),
            output_manager  = OutputManager(engine, output_dir=output_dir),
            sql_path        = sql_path,
            ddl_dir         = ddl_dir,
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

          1. Load schemas from ddl/*.csv
          2. Drop all managed tables (clean slate)
          3. Create tables from schema DDL
          4. Load input CSVs into tables
          5. Validate + execute transformation SQL
          6. Preview results (optional)
          7. Save CSVs + persist to DB (optional)

        Parameters
        ----------
        persist_to_db:
            Write output DataFrames to PostgreSQL. Default True.
        preview:
            Print a console preview of output tables. Default True.

        Returns
        -------
        ExecutionResult
            Contains the loaded schemas and all output DataFrames.
        """
        _banner("PostgreSQL SQL EXECUTOR")

        # ── Step 1-4: prepare the database ────────────────────────
        schemas = self._schema_manager.load_schemas(self.ddl_dir)
        self._schema_manager.drop_tables(schemas, sql_path=self.sql_path)
        self._schema_manager.create_tables(schemas)
        self._csv_ingester.load(self.data_dir, schemas)

        # ── Step 5: run the SQL ────────────────────────────────────
        output_tables = self._sql_runner.run(self.sql_path)

        # ── Step 6-7: present and persist results ─────────────────
        if preview:
            self._output_manager.preview(output_tables)

        self._output_manager.save_all(
            output_tables,
            persist_to_db=persist_to_db,
        )

        _banner("DONE")

        return ExecutionResult(schemas=schemas, output_tables=output_tables)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _banner(text: str) -> None:
    logger.info("=" * 70)
    logger.info(f"  {text}")
    logger.info("=" * 70)
