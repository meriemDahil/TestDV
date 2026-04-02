"""
tests/test_sql_executor_orchestration.py
-----------------------------------------
Unit tests for SQLExecutor.execute() — the pipeline orchestration method.
All four collaborators are mocked so no real DB or files are needed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from conftest import make_schema
from project.executor import SQLExecutor
from project.models   import ExecutionResult


def _make_executor(tmp_path, *, output_tables=None, sql_text="SELECT 1"):
    """
    Return (executor, mocks) with all collaborators replaced by MagicMock.
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
    def test_all_steps_are_called(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        executor.execute(persist_to_db=True, preview=True)

        m.sm.load_schemas.assert_called_once()
        m.sm.drop_tables.assert_called_once()
        m.sm.create_tables.assert_called_once()
        m.ci.load.assert_called_once()
        m.sr.run.assert_called_once()
        m.om.preview.assert_called_once()
        m.om.save_all.assert_called_once()

    def test_returns_execution_result_instance(self, tmp_path):
        executor, _ = _make_executor(tmp_path)
        result = executor.execute()
        assert isinstance(result, ExecutionResult)

    def test_result_carries_schemas_and_output_tables(self, tmp_path):
        tables  = {"out": pd.DataFrame({"a": [1]})}
        schemas = [make_schema("src", ["a"])]

        executor, m = _make_executor(tmp_path, output_tables=tables)
        m.sm.load_schemas.return_value = schemas

        result = executor.execute()

        assert result.schemas      is schemas
        assert result.output_tables is tables

    def test_preview_false_skips_preview_call(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        executor.execute(preview=False)
        m.om.preview.assert_not_called()

    def test_persist_to_db_false_is_forwarded_to_save_all(self, tmp_path):
        executor, m = _make_executor(tmp_path)
        executor.execute(persist_to_db=False)
        call_args = m.om.save_all.call_args
        # Accept both positional and keyword passing
        passed = (
            call_args.kwargs.get("persist_to_db")
            if call_args.kwargs
            else (call_args.args[1] if len(call_args.args) > 1 else None)
        )
        assert passed is False

    def test_schemas_passed_to_drop_tables(self, tmp_path):
        schemas = [make_schema("t", ["x"])]
        executor, m = _make_executor(tmp_path)
        m.sm.load_schemas.return_value = schemas

        executor.execute()

        drop_first_arg = m.sm.drop_tables.call_args.args[0]
        assert drop_first_arg is schemas

    def test_schemas_passed_to_create_tables(self, tmp_path):
        schemas = [make_schema("t", ["x"])]
        executor, m = _make_executor(tmp_path)
        m.sm.load_schemas.return_value = schemas

        executor.execute()

        create_first_arg = m.sm.create_tables.call_args.args[0]
        assert create_first_arg is schemas

    def test_sql_runner_receives_correct_sql_path(self, tmp_path):
        executor, m = _make_executor(tmp_path)
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
