"""
tests/test_sql_runner.py
-------------------------
Unit tests for SQLRunner.

The two most critical methods tested here:
  - split_statements  -- the sqlglot-powered SQL parser
  - _is_comment_only  -- the comment filter
  - _extract_insert_target -- the AST-based INSERT table extractor

All three are pure functions with no database dependency.
No engine, no PostgreSQL, no files needed.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock
from project.sql_runner import SQLRunner


# ── Fixture ───────────────────────────────────────────────────────

def make_runner() -> SQLRunner:
    """Return a SQLRunner with a mock engine and a no-op validator."""
    mock_engine = MagicMock()
    mock_validator = MagicMock()
    mock_validator.return_value.status.name = "PASSED"
    mock_validator.return_value.layer_name  = "0_validate_sql"
    mock_validator.return_value.duration_ms = 0.0
    mock_validator.return_value.checks      = []
    return SQLRunner(engine=mock_engine, sql_validator=mock_validator)


# ── split_statements ──────────────────────────────────────────────

class TestSplitStatements:

    def test_single_statement(self):
        runner = make_runner()
        result = runner.split_statements("SELECT 1")
        assert len(result) == 1
        assert "SELECT" in result[0].upper() and "1" in result[0]

    def test_two_statements(self):
        runner = make_runner()
        result = runner.split_statements("SELECT 1; SELECT 2")
        assert len(result) == 2

    def test_trailing_semicolon_no_empty_entry(self):
        runner = make_runner()
        result = runner.split_statements("SELECT 1;")
        assert len(result) == 1
        assert all(s.strip() for s in result)

    def test_semicolon_inside_single_quotes_not_split(self):
        runner = make_runner()
        sql = "SELECT 'hello;world' AS col"
        result = runner.split_statements(sql)
        assert len(result) == 1

    def test_semicolon_inside_double_quotes_not_split(self):
        runner = make_runner()
        # sqlglot normalises the quoted identifier — just verify it's one statement
        sql = 'SELECT "col;name" FROM t'
        result = runner.split_statements(sql)
        assert len(result) == 1

    def test_escaped_single_quote_not_split(self):
        """  'it''s a test;'  -- the '' is an escaped quote, not end of string """
        runner = make_runner()
        sql = "SELECT 'it''s a test;' AS col"
        result = runner.split_statements(sql)
        assert len(result) == 1

    def test_line_comment_semicolon_not_split(self):
        runner = make_runner()
        sql = "SELECT 1 -- this is; a comment\n, 2"
        result = runner.split_statements(sql)
        assert len(result) == 1

    def test_block_comment_semicolon_not_split(self):
        runner = make_runner()
        sql = "SELECT /* semi; inside */ 1"
        result = runner.split_statements(sql)
        assert len(result) == 1

    def test_multiline_statements(self):
        runner = make_runner()
        sql = """
            CREATE TABLE t (id INTEGER);
            INSERT INTO t VALUES (1);
            SELECT * FROM t
        """
        result = runner.split_statements(sql)
        assert len(result) == 3

    def test_empty_input_returns_empty_list(self):
        runner = make_runner()
        assert runner.split_statements("") == []
        assert runner.split_statements("   ") == []

    def test_only_whitespace_between_semicolons(self):
        runner = make_runner()
        result = runner.split_statements("SELECT 1;   ;SELECT 2")
        # the whitespace-only middle entry should be dropped
        assert len(result) == 2

    def test_to_char_date_format_not_split(self):
        """TO_CHAR(date, 'YYYY-MM-DD') -- string contains no semicolon but
        the format string should not confuse the parser."""
        runner = make_runner()
        sql = "SELECT TO_CHAR(dt, 'YYYY-MM-DD') FROM t"
        result = runner.split_statements(sql)
        assert len(result) == 1

    def test_real_multi_statement_script(self):
        runner = make_runner()
        sql = """
            DROP TABLE IF EXISTS out1;
            CREATE TABLE IF NOT EXISTS out1 (id TEXT, val REAL);
            INSERT INTO out1 SELECT id, val FROM src;
            SELECT * FROM out1;
        """
        result = runner.split_statements(sql)
        assert len(result) == 4

    def test_dollar_quoted_string_not_split(self):
        """PostgreSQL dollar-quoting: $$body with; semicolons$$ must not split."""
        runner = make_runner()
        sql = "SELECT $$hello;world$$ AS col"
        result = runner.split_statements(sql)
        assert len(result) == 1

    def test_schema_qualified_insert_parsed(self):
        runner = make_runner()
        sql = 'INSERT INTO public."Orders" SELECT id FROM src'
        result = runner.split_statements(sql)
        assert len(result) == 1


# ── _is_comment_only ─────────────────────────────────────────────

class TestIsCommentOnly:

    def test_real_sql_is_not_comment_only(self):
        runner = make_runner()
        assert not runner._is_comment_only("SELECT 1")

    def test_line_comment_only(self):
        runner = make_runner()
        assert runner._is_comment_only("-- this is a comment")

    def test_multiple_line_comments(self):
        runner = make_runner()
        stmt = """
            -- first comment
            -- second comment
        """
        assert runner._is_comment_only(stmt)

    def test_block_comment_only(self):
        runner = make_runner()
        assert runner._is_comment_only("/* block comment */")

    def test_mixed_comments_only(self):
        runner = make_runner()
        stmt = """
            -- line comment
            /* block comment */
        """
        assert runner._is_comment_only(stmt)

    def test_comment_plus_real_sql_is_not_comment_only(self):
        runner = make_runner()
        stmt = """
            -- component header
            SELECT * FROM t
        """
        assert not runner._is_comment_only(stmt)

    def test_empty_string_is_comment_only(self):
        runner = make_runner()
        assert runner._is_comment_only("")
        assert runner._is_comment_only("   ")

    def test_section_header_comment_is_comment_only(self):
        """Matches the style used in your SQL scripts."""
        stmt = """
            -- ----------------------------------------------------------------------
            -- Component : tMap_2_spec 1
            -- ----------------------------------------------------------------------
        """
        runner = make_runner()
        assert runner._is_comment_only(stmt)


# ── _extract_insert_target ────────────────────────────────────────

class TestExtractInsertTarget:

    def test_simple_insert(self):
        assert SQLRunner._extract_insert_target("INSERT INTO mytable SELECT 1") == "mytable"

    def test_schema_qualified_insert(self):
        assert SQLRunner._extract_insert_target(
            'INSERT INTO public."Orders" SELECT 1'
        ) == "Orders"

    def test_unquoted_schema_qualified_insert(self):
        assert SQLRunner._extract_insert_target(
            "INSERT INTO myschema.staging VALUES (1)"
        ) == "staging"

    def test_non_insert_returns_none(self):
        assert SQLRunner._extract_insert_target("SELECT 1") is None
        assert SQLRunner._extract_insert_target("UPDATE t SET x=1") is None
        assert SQLRunner._extract_insert_target("CREATE TABLE t (id INT)") is None

    def test_mixed_case_quoted_table(self):
        assert SQLRunner._extract_insert_target(
            'INSERT INTO "MySchema"."MyTable" SELECT 1'
        ) == "MyTable"

    def test_insert_values_form(self):
        assert SQLRunner._extract_insert_target(
            "INSERT INTO orders VALUES (1, 'x')"
        ) == "orders"
