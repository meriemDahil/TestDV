"""
tests/test_schema_manager_load_schemas.py
------------------------------------------
Unit tests for SchemaManager.load_schemas().
"""
from __future__ import annotations

from unittest.mock import MagicMock

from conftest import sqlite_engine, make_schema
from project.schema_manager import SchemaManager


class TestSchemaManagerLoadSchemas:
    def test_delegates_to_loader(self, tmp_path):
        schemas = [make_schema("orders", ["id", "amount"])]
        loader  = MagicMock(return_value=schemas)
        sm      = SchemaManager(engine=sqlite_engine(), schema_loader=loader)

        result = sm.load_schemas(tmp_path)

        loader.assert_called_once_with(tmp_path)
        assert result == schemas

    def test_returns_all_schemas(self, tmp_path):
        schemas = [
            make_schema("customers", ["id", "name"]),
            make_schema("orders",    ["id", "amount"]),
        ]
        sm = SchemaManager(engine=sqlite_engine(), schema_loader=lambda _: schemas)
        assert sm.load_schemas(tmp_path) == schemas

    def test_empty_directory_returns_empty_list(self, tmp_path):
        sm = SchemaManager(engine=sqlite_engine(), schema_loader=lambda _: [])
        assert sm.load_schemas(tmp_path) == []
