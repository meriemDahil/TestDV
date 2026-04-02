"""
tests/test_output_manager_save_all.py
--------------------------------------
Unit tests for OutputManager.save_all().
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from project.output_manager import OutputManager


def _engine():
    return create_engine("sqlite:///:memory:")


def _manager(tmp_path: Path, engine=None) -> OutputManager:
    return OutputManager(engine=engine or _engine(), output_dir=tmp_path / "out")


class TestOutputManagerSaveAll:
    def test_creates_output_dir_automatically(self, tmp_path):
        om = _manager(tmp_path)
        om.save_all({"t": pd.DataFrame({"a": [1]})}, persist_to_db=False)
        assert (tmp_path / "out").is_dir()

    def test_writes_one_csv_per_table(self, tmp_path):
        om = _manager(tmp_path)
        om.save_all(
            {
                "sales":   pd.DataFrame({"id": [1, 2]}),
                "refunds": pd.DataFrame({"id": [3]}),
            },
            persist_to_db=False,
        )
        assert (tmp_path / "out" / "sales.csv").exists()
        assert (tmp_path / "out" / "refunds.csv").exists()

    def test_csv_content_is_correct(self, tmp_path):
        om = _manager(tmp_path)
        df = pd.DataFrame({"x": [10, 20], "y": ["a", "b"]})
        om.save_all({"result": df}, persist_to_db=False)

        loaded = pd.read_csv(tmp_path / "out" / "result.csv")
        pd.testing.assert_frame_equal(loaded, df)

    def test_persist_to_db_true_writes_to_db(self, tmp_path):
        engine = _engine()
        om     = OutputManager(engine=engine, output_dir=tmp_path / "out")
        df     = pd.DataFrame({"val": [42]})

        om.save_all({"mytable": df}, persist_to_db=True)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM mytable")).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 42

    def test_persist_to_db_false_skips_db(self, tmp_path):
        engine = _engine()
        om     = OutputManager(engine=engine, output_dir=tmp_path / "out")
        om.save_all({"t": pd.DataFrame({"x": [1]})}, persist_to_db=False)

        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='t'")
            ).fetchone()
        assert result is None

    def test_extra_csv_paths_are_written(self, tmp_path):
        om         = _manager(tmp_path)
        extra_path = tmp_path / "extra" / "copy.csv"
        extra_path.parent.mkdir()

        om.save_all(
            {"t": pd.DataFrame({"n": [7]})},
            persist_to_db=False,
            extra_csv_paths={"t": extra_path},
        )

        assert extra_path.exists()
        assert pd.read_csv(extra_path)["n"].iloc[0] == 7

    def test_empty_tables_dict_is_no_op(self, tmp_path):
        om = _manager(tmp_path)
        om.save_all({}, persist_to_db=False)  # must not raise
