"""
tests/test_csv_ingester_validate_columns.py
--------------------------------------------
Unit tests for CSVIngester._validate_columns() — pure static method,
no engine or filesystem required.
"""
from __future__ import annotations

import pytest
from project.csv_ingester import CSVIngester


class TestCSVIngesterValidateColumns:
    def test_exact_match_passes(self):
        CSVIngester._validate_columns(["id", "name"], ["id", "name"], "f.csv")

    def test_wrong_order_passes(self):
        """Validation is set-based — column order is irrelevant."""
        CSVIngester._validate_columns(["name", "id"], ["id", "name"], "f.csv")

    def test_empty_both_passes(self):
        CSVIngester._validate_columns([], [], "f.csv")

    def test_missing_column_raises(self):
        with pytest.raises(ValueError, match="missing"):
            CSVIngester._validate_columns(["id"], ["id", "name"], "f.csv")

    def test_extra_column_raises(self):
        with pytest.raises(ValueError, match="unexpected"):
            CSVIngester._validate_columns(["id", "name", "extra"], ["id", "name"], "f.csv")

    def test_missing_and_extra_both_reported_in_one_error(self):
        with pytest.raises(ValueError) as exc_info:
            CSVIngester._validate_columns(["id", "extra"], ["id", "name"], "f.csv")
        msg = str(exc_info.value)
        assert "missing" in msg and "unexpected" in msg

    def test_filename_appears_in_error_message(self):
        with pytest.raises(ValueError, match="myfile.csv"):
            CSVIngester._validate_columns([], ["id"], "myfile.csv")

    def test_completely_wrong_columns_raises(self):
        with pytest.raises(ValueError):
            CSVIngester._validate_columns(["x", "y"], ["a", "b"], "bad.csv")
