"""
tests/test_output_manager_save_csv_persist.py
----------------------------------------------
Unit tests for OutputManager.save_csv() and OutputManager.persist().
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine, text

from project.output_manager import OutputManager


def _engine():
    return create_engine("sqlite:///:memory:")


class TestOutputManagerSaveCSV:
    def test_returns_correct_path(self, tmp_path):
        om   = OutputManager(engine=_engine(), output_dir=tmp_path)
        path = om.save_csv(pd.DataFrame({"a": [1]}), "report")
        assert path == tmp_path / "report.csv"

    def test_file_is_created(self, tmp_path):
        om = OutputManager(engine=_engine(), output_dir=tmp_path)
        om.save_csv(pd.DataFrame({"a": [1, 2]}), "out")
        assert (tmp_path / "out.csv").exists()

    def test_file_content_is_correct(self, tmp_path):
        om = OutputManager(engine=_engine(), output_dir=tmp_path)
        df = pd.DataFrame({"col": ["x", "y"]})
        om.save_csv(df, "data")
        loaded = pd.read_csv(tmp_path / "data.csv")
        pd.testing.assert_frame_equal(loaded, df)


class TestOutputManagerPersist:
    def test_writes_rows_to_db(self, tmp_path):
        engine = _engine()
        om     = OutputManager(engine=engine, output_dir=tmp_path)
        om.persist(pd.DataFrame({"v": [1, 2, 3]}), "tbl")

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM tbl")).fetchall()
        assert len(rows) == 3

    def test_second_persist_overwrites_first(self, tmp_path):
        """persist uses if_exists='replace' — no duplicate rows."""
        engine = _engine()
        om     = OutputManager(engine=engine, output_dir=tmp_path)

        om.persist(pd.DataFrame({"v": [1, 2]}), "tbl")
        om.persist(pd.DataFrame({"v": [99]}),   "tbl")

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM tbl")).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 99
