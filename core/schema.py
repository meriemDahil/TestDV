"""
core/schema.py
--------------
Reads a schema CSV (option A format):

    column_name,type
    order_id,VARCHAR
    revenue,FLOAT
    region,VARCHAR

And does two things:
  1. Generates a CREATE TABLE DDL string  → replaces dataSchema.sql
  2. Exposes column metadata              → used by the validation engine
     to know types and which column is the PK (if declared)

Supported type tokens (case-insensitive):
  Text    : VARCHAR, TEXT, STRING, CHAR, NVARCHAR
  Integer : INT, INTEGER, BIGINT, SMALLINT, TINYINT
  Float   : FLOAT, DOUBLE, REAL, NUMERIC, DECIMAL
  Boolean : BOOLEAN, BOOL
  Date    : DATE
  Datetime: DATETIME, TIMESTAMP
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd


# Type mapping  →  SQLite affinity

# SQLite only has 5 storage classes, but we keep the declared
# type in the DDL for readability and validation-layer use.
_SQLITE_AFFINITY: dict[str, str] = {
    # text
    "varchar":  "TEXT",
    "text":     "TEXT",
    "string":   "TEXT",
    "char":     "TEXT",
    "nvarchar": "TEXT",
    "clob":     "TEXT",
    # integer
    "int":       "INTEGER",
    "integer":   "INTEGER",
    "bigint":    "INTEGER",
    "smallint":  "INTEGER",
    "tinyint":   "INTEGER",
    "bool":      "INTEGER",   # SQLite stores booleans as 0/1
    "boolean":   "INTEGER",
    # float
    "float":     "REAL",
    "double":    "REAL",
    "real":      "REAL",
    "numeric":   "REAL",
    "decimal":   "REAL",
    # date/time  — stored as TEXT in SQLite
    "date":      "TEXT",
    "datetime":  "TEXT",
    "timestamp": "TEXT",
}

# Validation-engine dtype family (mirrors layer1_structural._dtype_family)
_VALIDATION_FAMILY: dict[str, str] = {
    "varchar":   "string",  "text":     "string",
    "string":    "string",  "char":     "string",
    "nvarchar":  "string",  "clob":     "string",
    "int":       "integer", "integer":  "integer",
    "bigint":    "integer", "smallint": "integer",
    "tinyint":   "integer",
    "bool":      "boolean", "boolean":  "boolean",
    "float":     "float",   "double":   "float",
    "real":      "float",   "numeric":  "float",
    "decimal":   "float",
    "date":      "date",
    "datetime":  "datetime","timestamp":"datetime",
}


# Column descriptor

@dataclass
class ColumnSchema:
    name:             str
    declared_type:    str          # exactly as written in the CSV
    sqlite_type:      str          # mapped SQLite affinity
    validation_family: str         # for the validation engine
    nullable:         bool = True
    is_primary_key:   bool = False


# Schema loader

@dataclass
class TableSchema:
    table_name: str
    columns:    list[ColumnSchema] = field(default_factory=list)

    # ── Public helpers ────────────────────────────────────────────────

    @property
    def primary_key_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.is_primary_key]

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_ddl(self) -> str:
        """
        Generates a SQLite-compatible CREATE TABLE statement.

        Example output:
            CREATE TABLE IF NOT EXISTS orders (
                order_id  TEXT,
                revenue   REAL,
                region    TEXT
            );
        """
        col_lines = []
        for col in self.columns:
            parts = [f"    {col.name:<24} {col.sqlite_type}"]
            if col.is_primary_key:
                parts.append("PRIMARY KEY")
            if not col.nullable:
                parts.append("NOT NULL")
            col_lines.append(" ".join(parts))

        body = ",\n".join(col_lines)
        return f"CREATE TABLE IF NOT EXISTS {self.table_name} (\n{body}\n);"

    def to_validation_config_hints(self) -> dict:
        """
        Returns a dict of hints the validation engine can use:
          - primary_key    → list[str]
          - column_types   → dict[col_name, family]

        Feed this into ValidationConfig if you want automatic config.
        """
        return {
            "primary_key":  self.primary_key_columns,
            "column_types": {c.name: c.validation_family for c in self.columns},
        }


class SchemaCSVLoader:
    """
    Reads a schema CSV file (option A) and produces a TableSchema.

    Minimum required columns in the CSV:
        column_name   — name of the column
        type          — declared SQL type (VARCHAR, FLOAT, etc.)

    Optional columns (if present, used automatically):
        nullable      — true/false  (default: true)
        primary_key   — true/false  (default: false)

    Example CSV:
        column_name,type,nullable,primary_key
        order_id,VARCHAR,false,true
        revenue,FLOAT,true,false
        region,VARCHAR,true,false
    """

    REQUIRED_COLS = {"column_name", "type"}

    def __init__(self, csv_path: str | Path, table_name: str = "output"):
        self.csv_path   = Path(csv_path)
        self.table_name = table_name

    def load(self) -> TableSchema:
        df = pd.read_csv(self.csv_path, keep_default_na=False)
        df.columns = [c.strip().lower() for c in df.columns]

        # ── Validate required columns ────────────────────────────────
        missing = self.REQUIRED_COLS - set(df.columns)
        if missing:
            raise ValueError(
                f"Schema CSV '{self.csv_path}' is missing required columns: {missing}. "
                f"Expected at minimum: {self.REQUIRED_COLS}."
            )

        columns: list[ColumnSchema] = []
        for _, row in df.iterrows():
            col_name      = str(row["column_name"]).strip()
            declared_type = str(row["type"]).strip()
            type_key      = declared_type.lower().split("(")[0].strip()  # strip VARCHAR(255) → varchar

            sqlite_type       = _SQLITE_AFFINITY.get(type_key, "TEXT")
            validation_family = _VALIDATION_FAMILY.get(type_key, "string")

            # Optional columns
            nullable = _parse_bool(row.get("nullable", "true"), default=True)
            is_pk    = _parse_bool(row.get("primary_key", "false"), default=False)

            if not col_name:
                raise ValueError(f"Empty column_name found in row: {dict(row)}")
            if type_key not in _SQLITE_AFFINITY:
                # Unknown type — warn but don't crash, fall back to TEXT
                import warnings
                warnings.warn(
                    f"Unknown type '{declared_type}' for column '{col_name}'. "
                    f"Falling back to TEXT. Supported types: {list(_SQLITE_AFFINITY.keys())}",
                    UserWarning,
                    stacklevel=2,
                )

            columns.append(ColumnSchema(
                name=col_name,
                declared_type=declared_type,
                sqlite_type=sqlite_type,
                validation_family=validation_family,
                nullable=nullable,
                is_primary_key=is_pk,
            ))

        return TableSchema(table_name=self.table_name, columns=columns)

    def load_and_write_ddl(self, output_path: str | Path) -> TableSchema:
        """
        Convenience method: load schema CSV → write DDL file → return TableSchema.
        Replaces dataSchema.sql entirely.
        """
        schema = self.load()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(schema.to_ddl(), encoding="utf-8")
        return schema

    @staticmethod
    def load_all_from_dir(ddl_dir: str | Path) -> list[TableSchema]:
        """
        Loads every *.csv in ddl_dir as a separate TableSchema.
        Filename without extension becomes the table name.

            ddl/
            ├── stg_tax_rates.csv    → TableSchema(table_name='stg_tax_rates')
            ├── stg_vendor_ref.csv   → TableSchema(table_name='stg_vendor_ref')
            └── stg_sap_input.csv    → TableSchema(table_name='stg_sap_input')

        Returns a list of TableSchema objects, one per file, sorted by filename.
        Raises FileNotFoundError if the directory doesn't exist or has no CSVs.
        """
        ddl_dir = Path(ddl_dir)

        if not ddl_dir.exists():
            raise FileNotFoundError(f"DDL directory not found: {ddl_dir}")

        csv_files = sorted(ddl_dir.glob("*.csv"))

        if not csv_files:
            raise FileNotFoundError(
                f"No CSV files found in DDL directory: {ddl_dir}. "
                f"Expected one CSV per table, e.g. stg_sap_input.csv"
            )

        schemas = []
        for csv_file in csv_files:
            table_name = csv_file.stem   # stg_sap_input.csv → stg_sap_input
            schema = SchemaCSVLoader(csv_file, table_name=table_name).load()
            schemas.append(schema)

        return schemas


# Helpers

def _parse_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return default