"""
run_executor.py
---------------
1. Reads schema from  ddl/*.csv            → one CSV per table, generates DDL for each
2. Loads CSVs from    data/*.csv           → populates each table
3. Executes SQL from  sql/transformations.sql → runs the transformation
4. Saves result to    data/testBench_output.csv
                  AND data/sql_generated_output.csv

DDL folder convention:
    ddl/
    ├── stg_tax_rates.csv     →  CREATE TABLE stg_tax_rates (...)
    ├── stg_vendor_ref.csv    →  CREATE TABLE stg_vendor_ref (...)
    └── stg_sap_input.csv     →  CREATE TABLE stg_sap_input (...)

Each CSV must have at minimum:
    column_name,type
    tax_code,TEXT
    tax_rate,REAL
"""

import sqlite3
import sys

import pandas as pd
from loguru import logger
from pathlib import Path
from sqlalchemy import create_engine
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.schema import SchemaCSVLoader, TableSchema


# Paths  — move to config later

BASE_DIR       = Path(__file__).parent.parent.resolve()
DDL_DIR        = BASE_DIR / "ddl"                             
SQL_PATH       = BASE_DIR / "sql_scripts" / "transformations.sql"
DATA_DIR       = BASE_DIR / "data"
TESTBENCH_OUT  = DATA_DIR / "testBench_output.csv"
SQL_G_OUT      = DATA_DIR / "sql_generated_output.csv"


# Steps

def load_all_schemas(ddl_dir: Path) -> list[TableSchema]:
    """
    Reads every *.csv in ddl/ — one file per table.
    Returns a list of TableSchema objects, one per table.
    """
    logger.info(f"Loading schemas from {ddl_dir}")
    schemas = SchemaCSVLoader.load_all_from_dir(ddl_dir)
    for s in schemas:
        pk_info = f", primary key: {s.primary_key_columns}" if s.primary_key_columns else ""
        logger.success(f"  {s.table_name}: {len(s.columns)} columns{pk_info}")
    return schemas


def create_tables(conn: sqlite3.Connection, schemas: list[TableSchema]) -> None:
    """
    Generates and executes one CREATE TABLE per schema.
    No .sql file needed — DDL is built from the CSV schemas.
    """
    for schema in schemas:
        ddl = schema.to_ddl()
        logger.debug(f"DDL for '{schema.table_name}':\n{ddl}")
        conn.executescript(ddl)
    conn.commit()
    logger.success(f"Created {len(schemas)} table(s): {[s.table_name for s in schemas]}")


def load_csvs(conn: sqlite3.Connection, data_dir: Path) -> None:
    """
    Loads every *.csv in data/ into a matching SQLite table.
    Filename without extension = table name.
    """
    for csv_file in sorted(data_dir.glob("*.csv")):
        table_name = csv_file.stem
        df = pd.read_csv(csv_file)
        df.to_sql(table_name, conn, if_exists="replace", index=False)
        logger.success(f"Loaded {csv_file.name} → table '{table_name}'  ({len(df):,} rows)")


def run_transformation(conn: sqlite3.Connection, sql_path: Path) -> pd.DataFrame:
    """Execute the transformation SQL and return the result as a DataFrame."""
    logger.info(f"Running transformation from {sql_path}")
    sql    = sql_path.read_text(encoding="utf-8")
    result = pd.read_sql_query(sql, conn)
    logger.success(f"Transformation complete — {len(result):,} rows returned")
    return result


def save_outputs(result: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    result.to_csv(TESTBENCH_OUT, index=False)
    result.to_csv(SQL_G_OUT,     index=False)
    logger.success(f"Saved Talend reference    → {TESTBENCH_OUT}")
    logger.success(f"Saved SQL staging output  → {SQL_G_OUT}")

    db_path = DATA_DIR / "pipeline.db"
    engine  = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    result.to_sql("sql_generated_output", engine, if_exists="replace", index=False)
    logger.success(f"Persisted 'sql_generated_output' → {db_path}")


def preview(result: pd.DataFrame) -> None:
    logger.info("Transformation result preview:")
    print(result.to_string(index=False))


# Main

def main() -> list[TableSchema]:
    logger.info("=" * 60)
    logger.info("  SQL EXECUTOR")
    logger.info("=" * 60)

    # 1. Load all schema CSVs from ddl/ → one TableSchema per table
    schemas = load_all_schemas(DDL_DIR)

    conn = sqlite3.connect(":memory:")
    try:
        # 2. Create all tables from generated DDL
        create_tables(conn, schemas)

        # 3. Load data CSVs into matching tables
        load_csvs(conn, DATA_DIR)

        # 4. Run transformation SQL
        result = run_transformation(conn, SQL_PATH)

        # 5. Preview + save
        preview(result)
        save_outputs(result)

    finally:
        conn.close()

    logger.info("=" * 60)
    logger.info("  Done.")
    logger.info("=" * 60)

    # Return schemas so the caller / validation engine can use them
    return schemas


if __name__ == "__main__":
    main()