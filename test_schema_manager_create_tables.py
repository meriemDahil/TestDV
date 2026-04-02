"""
tests/test_schema_manager_create_tables.py
-------------------------------------------
Unit tests for SchemaManager.create_tables().
"""
from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import create_engine, text

from conftest import make_schema
from project.schema_manager import SchemaManager


def _engine():
    return create_engine("sqlite:///:memory:")


class TestSchemaManagerCreateTables:
    def test_creates_single_table(self):
        engine  = _engine()
        schemas = [make_schema("customers", ["id", "name"])]
        sm      = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.create_tables(schemas)

        with engine.connect() as conn:
            conn.execute(text("SELECT * FROM customers")).fetchall()

    def test_creates_multiple_tables(self):
        engine  = _engine()
        schemas = [
            make_schema("customers", ["id", "name"]),
            make_schema("orders",    ["id", "amount"]),
        ]
        sm = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.create_tables(schemas)

        with engine.connect() as conn:
            conn.execute(text("SELECT * FROM customers")).fetchall()
            conn.execute(text("SELECT * FROM orders")).fetchall()

    def test_empty_schema_list_is_no_op(self):
        sm = SchemaManager(engine=_engine(), schema_loader=MagicMock())
        sm.create_tables([])  # must not raise

    def test_created_table_accepts_inserts(self):
        engine  = _engine()
        schemas = [make_schema("items", ["id", "val"])]
        sm      = SchemaManager(engine=engine, schema_loader=MagicMock())
        sm.create_tables(schemas)

        with engine.begin() as conn:
            conn.execute(text("INSERT INTO items VALUES ('1', 'hello')"))

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM items")).fetchall()
        assert len(rows) == 1
