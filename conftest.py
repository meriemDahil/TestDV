"""
tests/conftest.py
-----------------
Shared fixtures and helpers used across all test modules.
Pytest loads this automatically — no imports needed in test files.
"""
from __future__ import annotations

import sys
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

# ---------------------------------------------------------------------------
# Make project root importable.
# Adjust the path if your package layout differs.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Engine factory ────────────────────────────────────────────────────────

def sqlite_engine():
    """Return a fresh in-memory SQLite engine (no PostgreSQL required)."""
    return create_engine("sqlite:///:memory:")


# ── Minimal schema builder ────────────────────────────────────────────────

def _make_column(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def make_schema(table_name: str, columns: list[str], pk: list[str] | None = None):
    """
    Build a minimal TableSchema-like object that satisfies every
    pipeline component without importing the real SchemaCSVLoader.
    """
    return SimpleNamespace(
        table_name=table_name,
        columns=[_make_column(c) for c in columns],
        primary_key_columns=pk or [],
        to_ddl=lambda: (
            f"CREATE TABLE IF NOT EXISTS {table_name} "
            f"({', '.join(c + ' TEXT' for c in columns)})"
        ),
    )


# ── Pytest fixtures (available in every test file) ────────────────────────

@pytest.fixture
def engine():
    """Fresh SQLite in-memory engine per test."""
    return sqlite_engine()
