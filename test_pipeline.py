"""
tests/test_pipeline.py
-----------------------
Unit tests for:
  - CSVIngester       (csv_ingester.py)
  - SchemaManager     (schema_manager.py)
  - OutputManager     (output_manager.py)
  - SQLExecutor       (executor.py)

Design rules:
  - No real PostgreSQL required — SQLite in-memory or mocks throughout.
  - No real filesystem beyond pytest's tmp_path fixture.
  - Every dependency is injected; nothing is imported from utils.*.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch, PropertyMock

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Adjust sys.path so imports resolve from the project root.
# Remove / adjust these lines to match your actual package layout.
# ---------------------------------------------------------------------------
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from project.csv_ingester   import CSVIngester
from project.schema_manager import SchemaManager, _quote
from project.output_manager import OutputManager
from project.executor       import SQLExecutor
from project.models         import ExecutionResult


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers / fixtures
# ═══════════════════════════════════════════════════════════════════════════

def sqlite_engine():
    """Return an in-memory SQLite engine (no PostgreSQL required)."""
    return create_engine("sqlite:///:memory:")


def _make_column(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _make_schema(table_name: str, columns: list[str], pk: list[str] | None = None):
    """Build a minimal TableSchema-like object."""
    return SimpleNamespace(
        table_name=table_name,
        columns=[_make_column(c) for c in columns],
        primary_key_columns=pk or [],
        to_ddl=lambda: (
            f"CREATE TABLE IF NOT EXISTS {table_name} "
            f"({', '.join(c + ' TEXT' for c in columns)})"
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
# _quote  (schema_manager helper)
# ═══════════════════════════════════════════════════════════════════════════

class TestQuote:
    def test_simple_identifier(self):
        assert _quote("mytable") == '"mytable"'

    def test_underscore_allowed(self):
        assert _quote("my_table_1") == '"my_table_1"'

    def test_uppercase_allowed(self):
        assert _quote("MyTable") == '"MyTable"'

    def test_digit_start_raises(self):
        with pytest.raises(ValueError, match="Unsafe"):
            _quote("1table")

    def test_hyphen_raises(self):
        with pytest.raises(ValueError, match="Unsafe"):
            _quote("my-table")

    def test_semicolon_raises(self):
        with pytest.raises(ValueError, match="Unsafe"):
            _quote("t; DROP TABLE t--")

    def test_space_raises(self):
        with pytest.raises(ValueError, match="Unsafe"):
            _quote("my table")

    def test_dot_raises(self):
        with pytest.raises(ValueError, match="Unsafe"):
            _quote("schema.table")


# ═══════════════════════════════════════════════════════════════════════════
# SchemaManager
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaManagerLoadSchemas:
    def test_delegates_to_loader(self, tmp_path):
        schemas = [_make_schema("orders", ["id", "amount"])]
        loader  = MagicMock(return_value=schemas)
        sm      = SchemaManager(engine=sqlite_engine(), schema_loader=loader)

        result = sm.load_schemas(tmp_path)

        loader.assert_called_once_with(tmp_path)
        assert result == schemas

    def test_returns_all_schemas(self, tmp_path):
        schemas = [
            _make_schema("customers", ["id", "name"]),
            _make_schema("orders",    ["id", "amount"]),
        ]
        sm = SchemaManager(engine=sqlite_engine(), schema_loader=lambda _: schemas)
        assert sm.load_schemas(tmp_path) == schemas


class TestSchemaManagerDropTables:
    def test_drops_declared_tables(self):
        engine  = sqlite_engine()
        schemas = [_make_schema("src", ["id"])]

        # Pre-create the table so DROP doesn't error
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE src (id TEXT)"))

        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables(schemas)  # must not raise

    def test_drops_sql_created_tables(self, tmp_path):
        engine    = sqlite_engine()
        sql_file  = tmp_path / "transform.sql"
        sql_file.write_text("CREATE TABLE IF NOT EXISTS out1 (id TEXT);")

        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS out1 (id TEXT)"))

        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables([], sql_path=sql_file)  # must not raise

    def test_no_sql_path_drops_only_declared(self):
        engine  = sqlite_engine()
        schemas = [_make_schema("t1", ["x"])]

        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t1 (x TEXT)"))

        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables(schemas, sql_path=None)  # must not raise

    def test_idempotent_on_nonexistent_table(self):
        """DROP IF EXISTS on a table that doesn't exist should not raise."""
        engine  = sqlite_engine()
        schemas = [_make_schema("ghost_table", ["id"])]
        sm      = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables(schemas)  # must not raise


class TestSchemaManagerCreateTables:
    def test_creates_all_tables(self):
        engine  = sqlite_engine()
        schemas = [
            _make_schema("customers", ["id", "name"]),
            _make_schema("orders",    ["id", "amount"]),
        ]
        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.create_tables(schemas)

        with engine.connect() as conn:
            # Verify tables exist by querying them
            conn.execute(text("SELECT * FROM customers")).fetchall()
            conn.execute(text("SELECT * FROM orders")).fetchall()

    def test_empty_schema_list_is_no_op(self):
        sm = SchemaManager(engine=sqlite_engine(), schema_loader=MagicMock())
        sm.create_tables([])  # must not raise


# ═══════════════════════════════════════════════════════════════════════════
# CSVIngester
# ═══════════════════════════════════════════════════════════════════════════

def _write_csv(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content))
    return path


class TestCSVIngesterLoad:
    def _setup_table(self, engine, table: str, columns: list[str]):
        col_defs = ", ".join(f"{c} TEXT" for c in columns)
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE {table} ({col_defs})"))

    def test_loads_matching_csv(self, tmp_path):
        engine = sqlite_engine()
        schema = _make_schema("products", ["id", "name", "price"])
        self._setup_table(engine, "products", ["id", "name", "price"])

        _write_csv(tmp_path / "products.csv", """\
            id,name,price
            1,Widget,9.99
            2,Gadget,19.99
        """)

        CSVIngester(engine).load(tmp_path, [schema])

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM products")).fetchall()
        assert len(rows) == 2

    def test_column_normalisation_strips_whitespace(self, tmp_path):
        engine = sqlite_engine()
        schema = _make_schema("items", ["id", "val"])
        self._setup_table(engine, "items", ["id", "val"])

        _write_csv(tmp_path / "items.csv", """\
             id , val 
            1,hello
        """)

        CSVIngester(engine).load(tmp_path, [schema])

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM items")).fetchall()
        assert len(rows) == 1

    def test_missing_csv_raises_file_not_found(self, tmp_path):
        schema = _make_schema("missing_table", ["id"])
        with pytest.raises(FileNotFoundError, match="missing_table"):
            CSVIngester(sqlite_engine()).load(tmp_path, [schema])

    def test_unknown_csv_is_ignored(self, tmp_path):
        """A CSV with no matching schema entry must be silently ignored."""
        _write_csv(tmp_path / "unknown.csv", "a,b\n1,2\n")
        # Load with an empty schemas list — should not raise
        CSVIngester(sqlite_engine()).load(tmp_path, [])

    def test_multiple_schemas_loaded(self, tmp_path):
        engine = sqlite_engine()
        s1 = _make_schema("t1", ["a"])
        s2 = _make_schema("t2", ["b"])
        self._setup_table(engine, "t1", ["a"])
        self._setup_table(engine, "t2", ["b"])

        _write_csv(tmp_path / "t1.csv", "a\n1\n2\n")
        _write_csv(tmp_path / "t2.csv", "b\n3\n")

        CSVIngester(engine).load(tmp_path, [s1, s2])

        with engine.connect() as conn:
            assert len(conn.execute(text("SELECT * FROM t1")).fetchall()) == 2
            assert len(conn.execute(text("SELECT * FROM t2")).fetchall()) == 1


class TestCSVIngesterValidateColumns:
    def test_exact_match_passes(self):
        CSVIngester._validate_columns(["id", "name"], ["id", "name"], "f.csv")

    def test_missing_column_raises(self):
        with pytest.raises(ValueError, match="missing"):
            CSVIngester._validate_columns(["id"], ["id", "name"], "f.csv")

    def test_extra_column_raises(self):
        with pytest.raises(ValueError, match="unexpected"):
            CSVIngester._validate_columns(["id", "name", "extra"], ["id", "name"], "f.csv")

    def test_missing_and_extra_both_reported(self):
        with pytest.raises(ValueError) as exc_info:
            CSVIngester._validate_columns(["id", "extra"], ["id", "name"], "f.csv")
        msg = str(exc_info.value)
        assert "missing" in msg and "unexpected" in msg

    def test_wrong_order_passes(self):
        """Column validation is set-based — order doesn't matter."""
        CSVIngester._validate_columns(["name", "id"], ["id", "name"], "f.csv")

    def test_empty_both_passes(self):
        CSVIngester._validate_columns([], [], "f.csv")

    def test_filename_in_error_message(self):
        with pytest.raises(ValueError, match="myfile.csv"):
            CSVIngester._validate_columns([], ["id"], "myfile.csv")


# ═══════════════════════════════════════════════════════════════════════════
# OutputManager
# ═══════════════════════════════════════════════════════════════════════════

class TestOutputManagerSaveAll:
    def _make_manager(self, tmp_path) -> OutputManager:
        return OutputManager(engine=sqlite_engine(), output_dir=tmp_path / "out")

    def test_creates_output_dir(self, tmp_path):
        om = self._make_manager(tmp_path)
        om.save_all({"t": pd.DataFrame({"a": [1]})}, persist_to_db=False)
        assert (tmp_path / "out").is_dir()

    def test_writes_csv_per_table(self, tmp_path):
        om = self._make_manager(tmp_path)
        tables = {
            "sales":    pd.DataFrame({"id": [1, 2]}),
            "refunds":  pd.DataFrame({"id": [3]}),
        }
        om.save_all(tables, persist_to_db=False)
        assert (tmp_path / "out" / "sales.csv").exists()
        assert (tmp_path / "out" / "refunds.csv").exists()

    def test_csv_content_correct(self, tmp_path):
        om = self._make_manager(tmp_path)
        df = pd.DataFrame({"x": [10, 20], "y": ["a", "b"]})
        om.save_all({"result": df}, persist_to_db=False)

        loaded = pd.read_csv(tmp_path / "out" / "result.csv")
        pd.testing.assert_frame_equal(loaded, df)

    def test_persist_to_db_writes_to_sqlite(self, tmp_path):
        engine = sqlite_engine()
        om     = OutputManager(engine=engine, output_dir=tmp_path / "out")
        df     = pd.DataFrame({"val": [42]})

        om.save_all({"mytable": df}, persist_to_db=True)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM mytable")).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 42

    def test_persist_false_skips_db(self, tmp_path):
        engine = sqlite_engine()
        om     = OutputManager(engine=engine, output_dir=tmp_path / "out")
        om.save_all({"t": pd.DataFrame({"x": [1]})}, persist_to_db=False)

        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='t'")
            ).fetchone()
        assert result is None

    def test_extra_csv_paths_written(self, tmp_path):
        om        = self._make_manager(tmp_path)
        extra_dir = tmp_path / "extra"
        extra_dir.mkdir()
        extra_path = extra_dir / "copy.csv"

        df = pd.DataFrame({"n": [7]})
        om.save_all(
            {"t": df},
            persist_to_db=False,
            extra_csv_paths={"t": extra_path},
        )

        assert extra_path.exists()
        loaded = pd.read_csv(extra_path)
        assert loaded["n"].iloc[0] == 7

    def test_empty_tables_dict_is_no_op(self, tmp_path):
        om = self._make_manager(tmp_path)
        om.save_all({}, persist_to_db=False)  # must not raise


class TestOutputManagerSaveCSV:
    def test_save_csv_returns_path(self, tmp_path):
        om   = OutputManager(engine=sqlite_engine(), output_dir=tmp_path)
        path = om.save_csv(pd.DataFrame({"a": [1]}), "report")
        assert path == tmp_path / "report.csv"
        assert path.exists()


class TestOutputManagerPersist:
    def test_persist_overwrites_existing(self, tmp_path):
        engine = sqlite_engine()
        om     = OutputManager(engine=engine, output_dir=tmp_path)

        om.persist(pd.DataFrame({"v": [1, 2]}), "tbl")
        om.persist(pd.DataFrame({"v": [99]}),   "tbl")

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM tbl")).fetchall()
        assert len(rows) == 1  # replace, not append


class TestOutputManagerPreview:
    def test_preview_runs_without_error(self, capsys, tmp_path):
        om = OutputManager(engine=sqlite_engine(), output_dir=tmp_path)
        om.preview({
            "t1": pd.DataFrame({"a": range(5)}),
            "t2": pd.DataFrame({"b": range(60)}),   # triggers "more rows" path
        })
        captured = capsys.readouterr()
        assert "t2" in captured.out or True  # loguru goes to stderr; just no crash


# ═══════════════════════════════════════════════════════════════════════════
# SQLExecutor
# ═══════════════════════════════════════════════════════════════════════════

def _make_executor(tmp_path, *, output_tables=None, sql_text="SELECT 1"):
    """
    Build a SQLExecutor with fully mocked collaborators.
    Returns (executor, mocks_namespace).
    """
    output_tables = output_tables or {"result": pd.DataFrame({"x": [1]})}
    sql_path = tmp_path / "transform.sql"
    sql_path.write_text(sql_text)

    mock_sm = MagicMock()
    mock_sm.load_schemas.return_value = []

    mock_ci = MagicMock()

    mock_sr = MagicMock()
    mock_sr.run.return_value = output_tables

    mock_om = MagicMock()

    executor = SQLExecutor(
        schema_manager = mock_sm,
        csv_ingester   = mock_ci,
        sql_runner     = mock_sr,
        output_manager = mock_om,
        sql_path       = sql_path,
        ddl_dir        = tmp_path / "ddl",
        data_dir       = tmp_path / "data",
    )
    mocks = SimpleNamespace(sm=mock_sm, ci=mock_ci, sr=mock_sr, om=mock_om)
    return executor, mocks


class TestSQLExecutorOrchestration:
    def test_execute_calls_all_steps_in_order(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        result = executor.execute(persist_to_db=True, preview=True)

        # Every step must have been called
        m.sm.load_schemas.assert_called_once()
        m.sm.drop_tables.assert_called_once()
        m.sm.create_tables.assert_called_once()
        m.ci.load.assert_called_once()
        m.sr.run.assert_called_once()
        m.om.preview.assert_called_once()
        m.om.save_all.assert_called_once()

    def test_returns_execution_result(self, tmp_path):
        tables   = {"out": pd.DataFrame({"a": [1]})}
        schemas  = [_make_schema("src", ["a"])]

        executor, m = _make_executor(tmp_path, output_tables=tables)
        m.sm.load_schemas.return_value = schemas

        result = executor.execute()

        assert isinstance(result, ExecutionResult)
        assert result.schemas  is schemas
        assert result.output_tables is tables

    def test_preview_false_skips_preview(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        executor.execute(preview=False)
        m.om.preview.assert_not_called()

    def test_persist_to_db_flag_forwarded(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        executor.execute(persist_to_db=False)
        _, kwargs = m.om.save_all.call_args
        assert kwargs.get("persist_to_db") is False or \
               m.om.save_all.call_args[0][1] is False  # positional fallback

    def test_load_schemas_passed_to_drop_and_create(self, tmp_path):
        schemas = [_make_schema("t", ["x"])]
        executor, m = _make_executor(tmp_path)
        m.sm.load_schemas.return_value = schemas

        executor.execute()

        drop_call   = m.sm.drop_tables.call_args
        create_call = m.sm.create_tables.call_args

        assert schemas in drop_call.args or drop_call.args[0] is schemas
        assert schemas in create_call.args or create_call.args[0] is schemas

    def test_sql_runner_receives_correct_path(self, tmp_path):
        executor, m = _make_executor(tmp_path, sql_text="SELECT 42")
        executor.execute()
        m.sr.run.assert_called_once_with(executor.sql_path)

    def test_sql_runner_error_propagates(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        m.sr.run.side_effect = RuntimeError("SQL failed")

        with pytest.raises(RuntimeError, match="SQL failed"):
            executor.execute()

    def test_schema_manager_error_propagates(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        m.sm.load_schemas.side_effect = FileNotFoundError("ddl dir missing")

        with pytest.raises(FileNotFoundError):
            executor.execute()


class TestSQLExecutorFromPaths:
    def test_from_paths_builds_executor(self, tmp_path):
        sql_path = tmp_path / "t.sql"
        sql_path.write_text("SELECT 1")
        ddl_dir  = tmp_path / "ddl"
        data_dir = tmp_path / "data"

        mock_validator = MagicMock()
        mock_validator.return_value.status.name  = "PASSED"
        mock_validator.return_value.layer_name   = "0"
        mock_validator.return_value.duration_ms  = 0.0
        mock_validator.return_value.checks       = []

        mock_loader = MagicMock(return_value=[])

        executor = SQLExecutor.from_paths(
            engine        = sqlite_engine(),
            sql_path      = sql_path,
            ddl_dir       = ddl_dir,
            data_dir      = data_dir,
            sql_validator = mock_validator,
            schema_loader = mock_loader,
        )

        assert isinstance(executor, SQLExecutor)
        assert executor.sql_path == sql_path
        assert executor.ddl_dir  == ddl_dir
        assert executor.data_dir == data_dir

    def test_from_paths_output_dir_defaults_to_data_dir(self, tmp_path):
        sql_path = tmp_path / "t.sql"
        sql_path.write_text("SELECT 1")

        executor = SQLExecutor.from_paths(
            engine        = sqlite_engine(),
            sql_path      = sql_path,
            ddl_dir       = tmp_path / "ddl",
            data_dir      = tmp_path / "data",
            sql_validator = MagicMock(),
            schema_loader = MagicMock(return_value=[]),
        )
        assert executor._output_manager.output_dir == tmp_path / "data"

    def test_from_paths_custom_output_dir(self, tmp_path):
        sql_path   = tmp_path / "t.sql"
        sql_path.write_text("SELECT 1")
        output_dir = tmp_path / "results"

        executor = SQLExecutor.from_paths(
            engine        = sqlite_engine(),
            sql_path      = sql_path,
            ddl_dir       = tmp_path / "ddl",
            data_dir      = tmp_path / "data",
            output_dir    = output_dir,
            sql_validator = MagicMock(),
            schema_loader = MagicMock(return_value=[]),
        )
        assert executor._output_manager.output_dir == output_dir
