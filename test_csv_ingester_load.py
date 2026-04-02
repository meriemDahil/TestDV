"""
tests/test_csv_ingester_load.py
--------------------------------
Unit tests for CSVIngester.load() — the end-to-end CSV loading method.
Uses a real SQLite engine + pytest's tmp_path for CSV files.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from sqlalchemy import create_engine, text

from conftest import make_schema
from project.csv_ingester import CSVIngester


def _engine():
    return create_engine("sqlite:///:memory:")


def _write_csv(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content))
    return path


def _setup_table(engine, table: str, columns: list[str]) -> None:
    col_defs = ", ".join(f"{c} TEXT" for c in columns)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {table} ({col_defs})"))


class TestCSVIngesterLoad:
    def test_loads_matching_csv(self, tmp_path):
        engine = _engine()
        schema = make_schema("products", ["id", "name", "price"])
        _setup_table(engine, "products", ["id", "name", "price"])

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
        engine = _engine()
        schema = make_schema("items", ["id", "val"])
        _setup_table(engine, "items", ["id", "val"])

        _write_csv(tmp_path / "items.csv", """\
             id , val 
            1,hello
        """)

        CSVIngester(engine).load(tmp_path, [schema])

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM items")).fetchall()
        assert len(rows) == 1

    def test_missing_csv_raises_file_not_found(self, tmp_path):
        import pytest
        schema = make_schema("missing_table", ["id"])
        with pytest.raises(FileNotFoundError, match="missing_table"):
            CSVIngester(_engine()).load(tmp_path, [schema])

    def test_unknown_csv_without_schema_is_ignored(self, tmp_path):
        """A CSV file that has no matching schema entry is silently skipped."""
        _write_csv(tmp_path / "unknown.csv", "a,b\n1,2\n")
        CSVIngester(_engine()).load(tmp_path, [])  # must not raise

    def test_multiple_schemas_loaded(self, tmp_path):
        engine = _engine()
        s1 = make_schema("t1", ["a"])
        s2 = make_schema("t2", ["b"])
        _setup_table(engine, "t1", ["a"])
        _setup_table(engine, "t2", ["b"])

        _write_csv(tmp_path / "t1.csv", "a\n1\n2\n")
        _write_csv(tmp_path / "t2.csv", "b\n3\n")

        CSVIngester(engine).load(tmp_path, [s1, s2])

        with engine.connect() as conn:
            assert len(conn.execute(text("SELECT * FROM t1")).fetchall()) == 2
            assert len(conn.execute(text("SELECT * FROM t2")).fetchall()) == 1

    def test_columns_reordered_to_match_schema(self, tmp_path):
        """CSV column order must not matter — rows are reordered to schema order."""
        engine = _engine()
        schema = make_schema("things", ["id", "label"])
        _setup_table(engine, "things", ["id", "label"])

        # CSV has columns in reverse order
        _write_csv(tmp_path / "things.csv", "label,id\nalpha,1\n")

        CSVIngester(engine).load(tmp_path, [schema])

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, label FROM things")).fetchall()
        assert rows[0] == ("1", "alpha")
