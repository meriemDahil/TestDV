import os
import sqlite3
import pandas as pd
from pathlib import Path
from loguru import logger
from sqlalchemy import create_engine

"""
run_executor.py
---------------
1. Reads DDL from  /schema.sql        → creates tables in SQLite (in-memory)
2. Loads CSVs from data/*.csv            → populates each table by matching filename to table name
3. Executes SQL from sql/transformation.sql → runs the transformation
4. Saves result to data/testBench_output.csv  
   AND      to data/sql_generated_output.csv            
"""


# Paths   move this later to config 

BASE_DIR        = Path(__file__).parent.parent.resolve()  # project root
DDL_PATH        = BASE_DIR / "ddl"  / "dataSchema.sql"
SQL_PATH        = BASE_DIR / "sql_scripts"  / "transformations.sql"
DATA_DIR        = BASE_DIR / "data"
TESTBENCH_OUT      = DATA_DIR / "testBench_output.csv"
SQL_G_OUT         = DATA_DIR / "sql_generated_output.csv"

# Map CSV filename → table name (filename without .csv = table name)
# All CSVs in data/ that match a CREATE TABLE in the DDL are loaded automatically.


def load_ddl(conn: sqlite3.Connection, ddl_path: Path):
    """Execute the DDL script to create all tables."""
    logger.info(f"Loading DDL from {ddl_path}")
    ddl = ddl_path.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()
    logger.success("Tables created successfully")


def load_csvs(conn: sqlite3.Connection, data_dir: Path):

    # Get existing table names from the DB
    cursor     = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    db_tables  = {row[0] for row in cursor.fetchall()}

    for csv_file in sorted(data_dir.glob("*.csv")):
        table_name = csv_file.stem  #  dim_customer.csv → dim_customer
        df = pd.read_csv(csv_file)
        
        df.to_sql(table_name, conn, if_exists="replace", index=False)
        logger.success(f"Loaded {csv_file.name} → table '{table_name}'  ({len(df)} rows)")


def run_transformation(conn: sqlite3.Connection, sql_path: Path) -> pd.DataFrame:
    """Execute the transformation SQL and return the result as a DataFrame."""
    logger.info(f"Running transformation from {sql_path}")
    sql    = sql_path.read_text(encoding="utf-8")
    result = pd.read_sql_query(sql, conn)
    result.info()
    logger.success(f"Transformation complete — {len(result)} rows returned")
    return result



def save_outputs(result: pd.DataFrame):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(TESTBENCH_OUT, index=False)
    result.to_csv(SQL_G_OUT,    index=False)
    logger.success(f"Saved Talend reference → {TESTBENCH_OUT}")
    logger.success(f"Saved SQL staging output → {SQL_G_OUT}")


    os.makedirs(os.path.dirname("./data/pipeline.db"), exist_ok=True)
    engine = create_engine(
        f"sqlite:///{"./data/pipeline.db"}",
        connect_args={"check_same_thread": False},
        echo=False
        )
        # This will create or replace the 'stg_output' table
    result.to_sql("sql_generated_output", engine, if_exists="replace", index=False)
    logger.success(f"Persisted 'sql_generated_output' table to {"./data/pipeline.db"}")


def preview(result: pd.DataFrame):
    logger.info("Transformation result preview:")
    print(result.to_string(index=False))



# DB helper used by the comparator (load_table_as_dataframe shim)


def get_connection() -> sqlite3.Connection:
    """
    Returns a fresh in-memory SQLite connection with all tables loaded.
    Used by the comparator's db.py if you point it here.
    """
    conn = sqlite3.connect(":memory:")
    load_ddl(conn, DDL_PATH)
    load_csvs(conn, DATA_DIR)
    return conn


# Main


def main():
    logger.info("=" * 60)
    logger.info("  SQL EXECUTOR — mock pipeline")
    logger.info("=" * 60)

    conn = sqlite3.connect(":memory:")

    try:
        load_ddl(conn, DDL_PATH)
        load_csvs(conn, DATA_DIR)
        result = run_transformation(conn, SQL_PATH)
        preview(result)
        save_outputs(result)
    finally:
        conn.close()

    logger.info("=" * 60)
    logger.info("  Done.")
    logger.info("=" * 60)

    


if __name__ == "__main__":
    main()