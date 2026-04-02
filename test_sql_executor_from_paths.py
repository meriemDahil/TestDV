"""
tests/test_sql_executor_from_paths.py
--------------------------------------
Unit tests for SQLExecutor.from_paths() — the convenience factory method.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import create_engine

from project.executor import SQLExecutor


def _engine():
    return create_engine("sqlite:///:memory:")


def _base_executor(tmp_path, **overrides):
    """Build a SQLExecutor via from_paths with injectable overrides."""
    sql_path = tmp_path / "t.sql"
    sql_path.write_text("SELECT 1")

    defaults = dict(
        engine        = _engine(),
        sql_path      = sql_path,
        ddl_dir       = tmp_path / "ddl",
        data_dir      = tmp_path / "data",
        sql_validator = MagicMock(),
        schema_loader = MagicMock(return_value=[]),
    )
    defaults.update(overrides)
    return SQLExecutor.from_paths(**defaults)


class TestSQLExecutorFromPaths:
    def test_returns_executor_instance(self, tmp_path):
        executor = _base_executor(tmp_path)
        assert isinstance(executor, SQLExecutor)

    def test_sql_path_set_correctly(self, tmp_path):
        sql_path = tmp_path / "t.sql"
        sql_path.write_text("SELECT 1")
        executor = _base_executor(tmp_path, sql_path=sql_path)
        assert executor.sql_path == sql_path

    def test_ddl_dir_set_correctly(self, tmp_path):
        ddl_dir = tmp_path / "ddl"
        executor = _base_executor(tmp_path, ddl_dir=ddl_dir)
        assert executor.ddl_dir == ddl_dir

    def test_data_dir_set_correctly(self, tmp_path):
        data_dir = tmp_path / "data"
        executor = _base_executor(tmp_path, data_dir=data_dir)
        assert executor.data_dir == data_dir

    def test_output_dir_defaults_to_data_dir(self, tmp_path):
        data_dir = tmp_path / "data"
        executor = _base_executor(tmp_path, data_dir=data_dir)
        assert executor._output_manager.output_dir == data_dir

    def test_custom_output_dir_is_respected(self, tmp_path):
        output_dir = tmp_path / "results"
        executor   = _base_executor(tmp_path, output_dir=output_dir)
        assert executor._output_manager.output_dir == output_dir

    def test_validation_log_defaults_to_data_dir(self, tmp_path):
        data_dir = tmp_path / "data"
        executor = _base_executor(tmp_path, data_dir=data_dir)
        assert executor._sql_runner.validation_log_path == data_dir / "validation_log.json"

    def test_error_log_defaults_to_data_dir(self, tmp_path):
        data_dir = tmp_path / "data"
        executor = _base_executor(tmp_path, data_dir=data_dir)
        assert executor._sql_runner.error_log_path == data_dir / "error_log.json"

    def test_custom_validation_log_path(self, tmp_path):
        custom = tmp_path / "logs" / "val.json"
        executor = _base_executor(tmp_path, validation_log_path=custom)
        assert executor._sql_runner.validation_log_path == custom

    def test_custom_error_log_path(self, tmp_path):
        custom = tmp_path / "logs" / "err.json"
        executor = _base_executor(tmp_path, error_log_path=custom)
        assert executor._sql_runner.error_log_path == custom
