"""
Validation Engine – Orchestrator

Execution rules:
  1. Layer 1 (Structural) MUST pass before any other layer runs.
  2. Binary status only: PASSED or FAILED (+ SKIPPED for missing config).
  3. Skipped checks are reported with clear instructions on what's needed.
  4. AI narrative is generated last, from the completed report — never used for validation.

Usage:
    from engine import ValidationEngine
    from core.models import ValidationConfig

    config = ValidationConfig(
        talend_path="talend_output.csv",
        sql_path="sql_output.csv",
        primary_key=["order_id"],                       # optional
        aggregation_columns=["revenue", "qty"],         # optional
        group_by_columns=["region"],                    # optional
    )
    report = ValidationEngine(config).run()
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from typing import Self

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models import LayerResult, ValidationConfig, ValidationReport, Status
from core.normalize import SQLiteLoader, load_csv

from comparaison import layer1_stuctural
from comparaison import layer2_data
from comparaison import layer3_bizRules
from comparaison import layer4_stat
from comparaison.layer5_report import (
    build_summary, finalise_status,
    write_json_report, generate_ai_narrative,
    print_console_summary,
)


class ValidationEngine:

    def __init__(self, config: ValidationConfig):
        self.config = config
        self.db = SQLiteLoader()

    def run(
        self,
        source_df: pd.DataFrame | None = None,
        target_df: pd.DataFrame | None = None,
        anthropic_api_key: str = "",
        generate_narrative: bool = True,
    ) -> ValidationReport:

        t_start = time.perf_counter()
        report = ValidationReport.bootstrap(self.config)

        # ── Load & normalize ─────────────────────────────────────────
        print(f"[engine] Loading datasets …")
        try:
            if source_df is None:
                source_df = load_csv(self.config.talend_path)
            if target_df is None:
                target_df = load_csv(self.config.sql_path)

            self.db.load_both(source_df, target_df)
            print(f"[engine] {self.config.source_label}: {len(source_df):,} rows × {len(source_df.columns)} cols")
            print(f"[engine] {self.config.target_label}: {len(target_df):,} rows × {len(target_df.columns)} cols")

        except Exception as e:
            report.overall_status = Status.FAILED
            report.summary = {"load_error": str(e)}
            report.total_duration_ms = (time.perf_counter() - t_start) * 1000
            write_json_report(report, self.config.output_json_path)
            print_console_summary(report)
            return report

        # ── Layer 1: Structural (gate layer) ─────────────────────────
        print("[engine] Layer 1: Structural …")
        l1 = self._safe_run(layer1_stuctural.run, "1_structural")
        report.layers.append(l1)

        if not l1.passed:
            # STOP — structural mismatch makes all further checks meaningless
            print("[engine] Layer 1 FAILED — stopping. Fix structural issues first.")
            return self._finalise(report, t_start, anthropic_api_key, generate_narrative)

        # ── Layer 2: Data-level ──────────────────────────────────────
        print("[engine] Layer 2: Data-level …")
        l2 = self._safe_run(layer2_data.run, "2_data_level")
        report.layers.append(l2)

        # ── Layer 3: Business rules ──────────────────────────────────
        print("[engine] Layer 3: Business rules …")
        l3 = self._safe_run(layer3_bizRules.run, "3_business_rules")
        report.layers.append(l3)

        # ── Layer 4: Statistical ─────────────────────────────────────
        print("[engine] Layer 4: Statistical …")
        l4 = self._safe_run(layer4_stat.run, "4_statistical")
        report.layers.append(l4)

        return self._finalise(report, t_start, anthropic_api_key, generate_narrative)

    # ─────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────

    def _safe_run(self, layer_fn, layer_name: str) -> "LayerResult":
        from core.models import LayerResult, CheckResult
        try:
            return layer_fn(self.db, self.config)
        except Exception as e:
            tb = traceback.format_exc()
            result = LayerResult(layer_name=layer_name, status=Status.FAILED)
            result.add(CheckResult(
                layer=layer_name,
                check_name="layer_execution_error",
                status=Status.FAILED,
                message=f"Layer crashed unexpectedly: {e}",
                details={"traceback": tb},
            ))
            return result

    def _finalise(
        self,
        report: ValidationReport,
        t_start: float,
        api_key: str,
        generate_narrative: bool,
    ) -> ValidationReport:
        finalise_status(report)
        report.summary = build_summary(report)
        report.total_duration_ms = (time.perf_counter() - t_start) * 1000

        write_json_report(report, self.config.output_json_path)

        if generate_narrative:
            print("[engine] Generating AI narrative …")
            report.ai_narrative = generate_ai_narrative(report, api_key)
            write_json_report(report, self.config.output_json_path)

        print_console_summary(report)
        return report


if __name__ == "__main__":

    engine = ValidationEngine(ValidationConfig())
    report = engine.run(generate_narrative=False)  # set True + pass api_key when ready
