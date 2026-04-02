"""
tests/test_output_manager_preview.py
--------------------------------------
Unit tests for OutputManager.preview().
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine

from project.output_manager import OutputManager


def _engine():
    return create_engine("sqlite:///:memory:")


class TestOutputManagerPreview:
    def test_preview_does_not_raise(self, tmp_path):
        om = OutputManager(engine=_engine(), output_dir=tmp_path)
        om.preview({"t1": pd.DataFrame({"a": range(5)})})

    def test_preview_large_table_does_not_raise(self, tmp_path):
        """Tables > max_rows trigger the 'more rows' truncation path."""
        om = OutputManager(engine=_engine(), output_dir=tmp_path)
        om.preview({"big": pd.DataFrame({"b": range(60)})})

    def test_preview_multiple_tables_does_not_raise(self, tmp_path):
        om = OutputManager(engine=_engine(), output_dir=tmp_path)
        om.preview({
            "t1": pd.DataFrame({"a": [1, 2]}),
            "t2": pd.DataFrame({"b": [3, 4]}),
        })

    def test_preview_empty_dict_is_no_op(self, tmp_path):
        om = OutputManager(engine=_engine(), output_dir=tmp_path)
        om.preview({})  # must not raise

    def test_preview_prints_to_stdout(self, tmp_path, capsys):
        om = OutputManager(engine=_engine(), output_dir=tmp_path)
        om.preview({"results": pd.DataFrame({"x": [42]})})
        # DataFrame.to_string() writes to stdout
        captured = capsys.readouterr()
        assert "42" in captured.out
