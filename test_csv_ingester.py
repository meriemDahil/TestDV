"""
tests/test_csv_ingester.py
---------------------------
Unit tests for CSVIngester._validate_columns.

This is a static method — completely pure, no database, no files.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pipeline.csv_ingester import CSVIngester


class TestValidateColumns:

    def test_exact_match_passes(self):
        # Should not raise
        CSVIngester._validate_columns(
            actual   = ["id", "name", "country"],
            expected = ["id", "name", "country"],
            filename = "test.csv",
        )

    def test_different_order_passes(self):
        # Order doesn't matter for validation — only presence
        CSVIngester._validate_columns(
            actual   = ["country", "id", "name"],
            expected = ["id", "name", "country"],
            filename = "test.csv",
        )

    def test_missing_column_raises(self):
        with pytest.raises(ValueError, match="missing columns"):
            CSVIngester._validate_columns(
                actual   = ["id", "name"],
                expected = ["id", "name", "country"],
                filename = "customer.csv",
            )

    def test_extra_column_raises(self):
        with pytest.raises(ValueError, match="unexpected columns"):
            CSVIngester._validate_columns(
                actual   = ["id", "name", "country", "ghost"],
                expected = ["id", "name", "country"],
                filename = "customer.csv",
            )

    def test_both_missing_and_extra_raises(self):
        with pytest.raises(ValueError):
            CSVIngester._validate_columns(
                actual   = ["id", "ghost"],
                expected = ["id", "name"],
                filename = "customer.csv",
            )

    def test_error_message_contains_filename(self):
        with pytest.raises(ValueError, match="orders.csv"):
            CSVIngester._validate_columns(
                actual   = ["id"],
                expected = ["id", "revenue"],
                filename = "orders.csv",
            )

    def test_error_message_contains_missing_column_name(self):
        with pytest.raises(ValueError, match="revenue"):
            CSVIngester._validate_columns(
                actual   = ["id"],
                expected = ["id", "revenue"],
                filename = "test.csv",
            )

    def test_empty_both_passes(self):
        CSVIngester._validate_columns([], [], "empty.csv")

    def test_case_sensitive(self):
        """'Country' and 'country' are different — must raise."""
        with pytest.raises(ValueError):
            CSVIngester._validate_columns(
                actual   = ["id", "Country"],
                expected = ["id", "country"],
                filename = "customer.csv",
            )
