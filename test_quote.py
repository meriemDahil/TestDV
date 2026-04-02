"""
tests/test_quote.py
--------------------
Unit tests for the _quote() identifier-safety helper in schema_manager.py.
Pure function — no engine, no filesystem.
"""
from __future__ import annotations

import pytest
from project.schema_manager import _quote


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
