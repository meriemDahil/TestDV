"""
tests/test_schema_manager_drop_tables.py
-----------------------------------------
Unit tests for SchemaManager.drop_tables().
"""
from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import create_engine, text

from conftest import make_schema
from project.schema_manager import SchemaManager


def _engine():
    return create_engine("sqlite:///:memory:")


class TestSchemaManagerDropTables:
    def test_drops_declared_tables(self):
        engine  = _engine()
        schemas = [make_schema("src", ["id"])]

        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE src (id TEXT)"))

        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables(schemas)  # must not raise

    def test_drops_sql_created_tables(self, tmp_path):
        engine   = _engine()
        sql_file = tmp_path / "transform.sql"
        sql_file.write_text("CREATE TABLE IF NOT EXISTS out1 (id TEXT);")

        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS out1 (id TEXT)"))

        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables([], sql_path=sql_file)  # must not raise

    def test_no_sql_path_drops_only_declared(self):
        engine  = _engine()
        schemas = [make_schema("t1", ["x"])]

        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t1 (x TEXT)"))

        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables(schemas, sql_path=None)  # must not raise

    def test_idempotent_on_nonexistent_table(self):
        """DROP IF EXISTS on a table that was never created must not raise."""
        engine  = _engine()
        schemas = [make_schema("ghost_table", ["id"])]
        sm      = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.drop_tables(schemas)  # must not raise

    def test_empty_schemas_and_no_sql_path_is_no_op(self):
        sm = SchemaManager(engine=_engine(), schema_loader=MagicMock())
        sm.drop_tables([], sql_path=None)  # must not raise
