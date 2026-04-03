"""
PATCH for sql_runner.py
------------------------
Add this single static method to the SQLRunner class, directly below
the existing `created_tables()` instance method (around line 55).

This gives InferredCSVLoader a path-based entry point so it can detect
output tables without instantiating a full SQLRunner.
"""

    @staticmethod
    def created_tables_from_path(sql_path: Path) -> set[str]:
        """
        Return the set of table names created by the SQL script at sql_path.
        Static version of created_tables() — accepts a Path directly so
        InferredCSVLoader can call it without a full SQLRunner instance.
        """
        if not sql_path or not sql_path.exists():
            return set()

        sql_script = sql_path.read_text(encoding="utf-8")
        nodes = sqlglot.parse(
            sql_script, dialect=_DIALECT, error_level=sqlglot.ErrorLevel.WARN
        )
        return {
            tbl
            for node in nodes
            if node is not None
            for tbl in [SQLRunner._extract_created_table(node.sql(dialect=_DIALECT))]
            if tbl
        }
