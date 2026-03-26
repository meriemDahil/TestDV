"""
pipeline/models.py
------------------
Shared data structures used across all pipeline components.
No business logic here — only types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, create_engine


# ─────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """
    Structured result returned by the full pipeline execution.

    Attributes
    ----------
    schemas:
        TableSchema objects loaded from ddl/*.csv — one per source table.
    output_tables:
        Every table populated by INSERT INTO in the SQL script,
        keyed by table name, valued as the DataFrame snapshot.
    """
    schemas:       list                        # list[TableSchema]
    output_tables: dict[str, pd.DataFrame]     = field(default_factory=dict)


# ─────────────────────────────────────────────
# Engine factory
# ─────────────────────────────────────────────

def make_postgres_engine(
    host:     str = "localhost",
    port:     int = 5432,
    user:     str = "",
    password: str = "",
    dbname:   str = "mydb",
    encoding: str = "utf8",
) -> Engine:
    """
    Build a SQLAlchemy PostgreSQL engine from explicit parameters.
    Credentials are passed in — never hardcoded in the class.

    Usage
    -----
    engine = make_postgres_engine(
        user=settings.USER,
        password=settings.PASSWORD,
    )
    executor = SQLExecutor(engine=engine, ...)
    """
    return create_engine(
        "postgresql+psycopg2://",
        connect_args={
            "host":            host,
            "port":            port,
            "user":            user,
            "password":        password,
            "dbname":          dbname,
            "client_encoding": encoding,
            "options":         f"-c client_encoding={encoding.upper()}",
        },
    )
