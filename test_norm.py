from __future__ import annotations

import chardet
import pandas as pd
import unidecode


# ============================================================
# COLUMN NORMALIZATION
# ============================================================

class ColumnNormalizer:
    """
    Standardizes column names into predictable snake_case form.

    Examples:
        'First Name ' → 'first_name'
        'Montant-HT'  → 'montant_ht'
    """

    @staticmethod
    def normalize(name: str) -> str:
        return (
            unidecode.unidecode(name)
            .strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
        )

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        normalized = [self.normalize(str(c)) for c in df.columns]

        # Deduplicate names created after normalization
        seen: dict[str, int] = {}
        deduped = []

        for col in normalized:
            if col in seen:
                seen[col] += 1
                deduped.append(f"{col}_{seen[col]}")
            else:
                seen[col] = 0
                deduped.append(col)

        df.columns = deduped
        return df


# ============================================================
# NULL NORMALIZATION
# ============================================================

class NullNormalizer:
    NULL_VALUES = {
        "", " ", "null", "Null", "NULL",
        "none", "None", "NONE",
        "na", "NA", "n/a", "N/A",
        "?", "nan", "NaN", "NAN",
        "nil", "n.a.", "nd", "#n/a", "#N/A"
    }

    @classmethod
    def _norm(cls, v):
        if v is None:
            return None

        text = str(v).strip().lower()
        return None if text in cls.NULL_VALUES else v

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Universal implementation — works on ALL pandas versions.
        Uses Series.map instead of DataFrame.applymap.
        """
        df = df.copy()
        for col in df.columns:
            df[col] = df[col].map(self._norm)
        return df


# ============================================================
# STRING TRIMMING
# ============================================================

class StringTrimNormalizer:
    """Trim whitespace from object/string columns."""

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in df.select_dtypes(include=["object", "string"]).columns:
            df[col] = df[col].astype(str).str.strip()
        return df


# ============================================================
# DATE NORMALIZATION (SAFE VERSION)
# ============================================================

class DateNormalizer:
    """
    Safely convert date-like columns into pandas datetime.

    Protects:
      • YEAR-only columns ("2020") → NOT treated as dates.
      • Prevents datetime → float issues during numeric inference.
    """

    NAME_HINTS = ("date", "time", "timestamp", "_at")
    MIN_SUCCESS_RATIO = 0.8
    MIN_SAMPLE = 20

    @classmethod
    def _has_date_hint(cls, col_name: str) -> bool:
        return any(token in col_name.lower() for token in cls.NAME_HINTS)

    @classmethod
    def _looks_like_full_date(cls, series: pd.Series) -> bool:
        non_null = series.dropna().astype(str)

        # Reject if >50% are pure 4-digit years
        year_like = non_null.str.fullmatch(r"\d{4}").sum()
        if year_like / len(non_null) > 0.5:
            return False

        return True

    @classmethod
    def _should_convert(cls, col_name: str, series: pd.Series) -> bool:
        if series.dtype.kind != "O":  # Only string/object
            return False

        non_null = series.dropna()
        if len(non_null) < cls.MIN_SAMPLE:
            return False

        if not cls._looks_like_full_date(series):
            return False

        if cls._has_date_hint(col_name):
            return True

        # Check success ratio
        parsed = pd.to_datetime(non_null, errors="coerce", dayfirst=True, format="mixed")
        success_ratio = parsed.notna().sum() / len(non_null)

        return success_ratio >= cls.MIN_SUCCESS_RATIO

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        for col in df.columns:
            if self._should_convert(str(col), df[col]):
                df[col] = pd.to_datetime(
                    df[col],
                    errors="coerce",
                    dayfirst=True,
                    format="mixed",
                )

        return df


# ============================================================
# NORMALIZATION PIPELINE
# ============================================================

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = ColumnNormalizer().apply(df)
    df = NullNormalizer().apply(df)
    df = StringTrimNormalizer().apply(df)
    df = DateNormalizer().apply(df)
    return df


# ============================================================
# CSV READER (ENCODING + DELIMITER AUTODETECTION)
# ============================================================

class CSVReader:
    SEPARATORS = [",", ";", "\t", "|"]

    def __init__(self, path: str):
        self.path = path

    def detect_encoding(self) -> str:
        with open(self.path, "rb") as f:
            raw = f.read(50_000)
        return chardet.detect(raw)["encoding"] or "utf-8"

    def detect_separator(self, encoding: str) -> str:
        with open(self.path, "r", encoding=encoding, errors="replace") as f:
            sample = f.read(4096)
        counts = {sep: sample.count(sep) for sep in self.SEPARATORS}
        return max(counts, key=counts.get)

    def read(self) -> pd.DataFrame:
        encoding = self.detect_encoding()
        sep = self.detect_separator(encoding)
        return pd.read_csv(
            self.path,
            encoding=encoding,
            sep=sep,
            low_memory=False,
            keep_default_na=False,
        )


# ============================================================
# DATE-LIKE COLUMN GUARD — centralised helper
# ============================================================

# FIX: Single source of truth for all name-based date/time hints.
# Previously these were scattered inline inside load_csv, causing
# inconsistent checks and allowing some date columns to leak through
# into the numeric inference block.
_DATE_HINTS = ("date", "time", "timestamp", "_at", "year")


def _is_date_like_column(col_name: str, series: pd.Series) -> bool:
    """
    Return True if this column should be treated as a date/temporal
    column and therefore SKIPPED by numeric inference.

    Checks (in order):
    1. Column already holds a parsed datetime dtype.
    2. Column name contains a known date-related hint.
    3. All non-null values look like 4-digit year strings (e.g. '2021').
    4. All non-null values look like float-encoded years (e.g. 2021.0
       where the fractional part is .0) — this is the key regression
       case: when a year column sneaks through as float64 it must still
       be recognised and skipped here so we don't re-cast it as a plain
       number.
    """
    # 1. Already a datetime
    if pd.api.types.is_datetime64_any_dtype(series):
        return True

    name = col_name.lower()

    # 2. Name hint
    if any(hint in name for hint in _DATE_HINTS):
        return True

    non_null = series.dropna()
    if non_null.empty:
        return False

    # 3. Year-like strings: '2021', '1999', …
    as_str = non_null.astype(str)
    if as_str.str.fullmatch(r"\d{4}").mean() > 0.5:
        return True

    # 4. FIX — Year-like floats that slipped through as float64: 2021.0
    #    This happens when DateNormalizer skipped a small dataset
    #    (MIN_SAMPLE not met) and numeric inference ran first.
    #    We detect them here so load_csv doesn't re-infer them as plain
    #    numbers, which would strip the temporal semantics entirely.
    if pd.api.types.is_float_dtype(series):
        # Check: integer part is in plausible year range AND fractional
        # part is always zero (i.e. these are really integers stored as float)
        numeric_vals = pd.to_numeric(non_null, errors="coerce").dropna()
        if not numeric_vals.empty:
            frac_is_zero = (numeric_vals % 1 == 0).all()
            in_year_range = numeric_vals.between(1000, 9999).mean() > 0.5
            if frac_is_zero and in_year_range:
                return True

    return False


# ============================================================
# HIGH-LEVEL CSV LOADER
# ============================================================

def load_csv(path: str) -> pd.DataFrame:
    """
    High-level CSV loader with safe normalization and numeric inference.

    Key fix: numeric inference now delegates the date/temporal guard to
    _is_date_like_column(), which handles all four cases — datetime
    dtype, name hints, year-string columns, and year-as-float columns —
    from a single, consistent place. The original code had duplicated
    datetime checks and an incomplete name-hint list, which allowed
    year values to be downcast to float64 before reaching the SQL layer.
    """
    df = pd.read_csv(path)
    df = normalize(df)

    for col in df.columns:

        # FIX: replaced the duplicated / inconsistent guards with a
        # single call to _is_date_like_column().
        if _is_date_like_column(str(col), df[col]):
            continue

        non_null_str = df[col].dropna().astype(str)

        # Skip columns that are entirely empty after normalization
        if non_null_str.empty:
            continue

        # Numeric inference
        numeric = pd.to_numeric(df[col], errors="coerce")
        if numeric.notna().sum() >= (df[col].notna().sum() * 0.5):
            df[col] = numeric

    return df











"""
run_validation_agent.py
=======================

Two-phase validation pipeline:

PHASE 1 — Simulation validation
 1. Execute code against DataGenAgent test data
 2. Store results in output_validation_agent/simulation_val/
 3. Compare simulation_val vs output_test_bench_agent/SimulationAgent/
 4. Generate per-pair reports + validation_log_simulation.json
 5. On failure → correction loop (max MAX_CORRECTION_ITERATIONS = 5)
 • corrected file persisted as output_correction_agent/transformations.sql
 • each iteration overwrites transformations.sql with the latest attempt

PHASE 2 — Java validation
 1. Execute best available code from phase 1 (corrected or original)
    against DataGenAgent test data
 2. Store results in output_validation_agent/java_val/
 3. Compare java_val vs output_java_execution_agent/
 4. Generate per-pair reports + validation_log_java.json
 5. On failure → correction loop (max MAX_CORRECTION_ITERATIONS = 5)
 • corrected file persisted as output_correction_agent/transformations_java.sql

RESULT
 • If either phase succeeds the orchestrator receives "success" with
   winning_code / winning_path pointing to the successful SQL.
 • Phase 2 winner is preferred over phase 1 when both succeed (stricter).
 • Every intermediate file is kept; nothing is silently discarded.

FILE GENERATION GUARANTEES
 • transformations.sql is written (and overwritten) on every iteration of
   phase 1 correction — always reflects the latest attempt.
 • transformations_java.sql follows the same guarantee for phase 2.
 • If CorrectionAgent returns empty code the previous file on disk is
   kept intact and the iteration is logged as failed.
 • No per-iteration sub-folders are created; all re-execution outputs
   go directly into simulation_val / java_val respectively.
 • validation_log_simulation.json and validation_log_java.json are
   overwritten after each iteration — no _after_correction variants.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from os import PathLike
from pathlib import Path

from migration_factory_backend.modules.correction.correction_agent import CorrectionAgent
from migration_factory_backend.modules.systems.code_system_registry import (
    get_output_system_config,
)
from migration_factory_backend.utils.logger import initLogger
from migration_factory_backend.utils.validation_agent.execution.generic_executor import (
    execute_code,
)
from migration_factory_backend.utils.validation_agent.helpers.models import (
    ValidationConfig,
    ValidationReport,
)
from migration_factory_backend.utils.validation_agent.helpers.normalize import load_csv
from scripts.scripts_validation_agent.validation_engine import (
    ValidationEngine,
    discover_comparisons,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = initLogger("ValidationAgent")

MAX_CORRECTION_ITERATIONS: int = 5


# ─────────────────────────────────────────────────────────────────────────────
# Low-level helpers
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_code_path(
    run_path: Path,
    output_system: str,
    use_correction: bool,
    code_path: Path | PathLike | None,
) -> Path:
    """Return the path of the code file that should be validated."""
    if code_path:
        return Path(code_path)
    cfg = get_output_system_config(output_system)
    if use_correction:
        return (
            run_path
            / "output"
            / "output_correction_agent"
            / cfg["correction_file"]
        )
    return run_path / "output" / cfg["output_dir"] / cfg["final_file"]


def _read_code_safe(path: Path) -> str:
    """Read *path* as UTF-8 text; return '' and log a warning on any error."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Could not read code file: %s", path)
        return ""


def _persist_validation_log(path: Path, log: dict) -> None:
    """Write *log* as JSON to *path*, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Validation log written → %s", path)


def _build_failure_payload(
    code_query: str,
    output_system: str,
    error: Exception | str,
    issue_type: str | None = None,
) -> dict:
    """Construct a standardised failure payload dict."""
    error_str = str(error)
    if issue_type is None:
        if (
            "JAVA_HOME" in error_str
            or "Java not found" in error_str
            or "JAVA_GATEWAY_EXITED" in error_str
            or "No module named 'pyspark'" in error_str
            or "PySpark environment check failed" in error_str
        ):
            issue_type = "environment"
        else:
            issue_type = "runtime"
    return {
        "sql_query": code_query,
        "code_query": code_query,
        "output_system": output_system,
        "status": "failure",
        "issue": {
            "details": f"Pipeline execution failed: {error_str}",
            "type": issue_type,
        },
    }


def _extract_status(log_or_payload: dict) -> str:
    """
    Safely pull status from either a raw payload or an envelope dict
    {output: {...}, error: N}. Returns 'failure' when absent.
    """
    if not isinstance(log_or_payload, dict):
        return "failure"
    inner = (
        log_or_payload.get("output")
        or log_or_payload.get("result")
        or log_or_payload
    )
    if isinstance(inner, dict):
        return inner.get("status", "failure")
    return "failure"


def _write_corrected_file(path: Path, code: str, iteration: int, phase_label: str) -> None:
    """
    Write *code* to *path* (the canonical corrected-file location), overwriting
    any previous iteration.  No archive sub-folders are created.

    This is the single place that writes correction SQL files. Calling it
    guarantees the file is on disk regardless of what happens in re-execution.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    logger.info(
        "[%s] Corrected file written → %s (iteration %d)",
        phase_label,
        path,
        iteration,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Narrative helper (re-used in both the phase runner and the correction loop)
# ─────────────────────────────────────────────────────────────────────────────


def _run_narrative(
    reports_data: list[dict],
    code_query: str,
    output_system: str,
) -> dict:
    """
    Feed *reports_data* through NarrativeService and return a normalised
    envelope {output: {...}, error: 0|1}.

    BUG FIXED: previously the error flag was read back from the narrative
    result *after* the payload had been extracted from it, always defaulting
    to 0. Now the error flag is derived from the payload status directly so
    a failure report is never mis-tagged as error=0.
    """
    if not reports_data:
        payload = _build_failure_payload(code_query, output_system, "no reports generated")
        return {"output": payload, "error": 1}

    from scripts.scripts_validation_agent.narrative_service import NarrativeService

    narrative_service = NarrativeService(ValidationConfig(), logger=logger)
    raw_log = narrative_service.run(reports=reports_data, sql_query=code_query)

    payload = (
        raw_log.get("output")
        or raw_log.get("result")
        or raw_log
    )

    if isinstance(payload, dict):
        payload["code_query"] = code_query
        payload["output_system"] = output_system
        payload.setdefault("sql_query", code_query)
    else:
        payload = _build_failure_payload(code_query, output_system, "narrative returned non-dict")

    # Derive error flag from status — do NOT read it back from raw_log
    # which may have been mutated or may default to 0 regardless of status.
    error_flag = 0 if payload.get("status", "failure").lower() == "success" else 1

    return {"output": payload, "error": error_flag}


# ─────────────────────────────────────────────────────────────────────────────
# Comparison helper (re-used in both the phase runner and the correction loop)
# ─────────────────────────────────────────────────────────────────────────────


def _run_comparisons(
    *,
    exec_output_dir: Path,
    compare_against_dir: Path,
    code_file_path: Path,
    validation_output_dir: Path,
    report_prefix: str,
    phase_label: str,
) -> list[dict]:
    """
    Discover output/expected pairs, run ValidationEngine on each, persist
    per-pair JSON reports, and return the list of loaded report dicts.

    Returns [] when no pairs are found (caller must treat this as failure).
    Reports are written directly into *validation_output_dir* (the stable
    phase dir — simulation_val or java_val), overwriting previous reports
    with the same name so no iteration sub-folders accumulate.
    """
    comparisons = discover_comparisons(exec_output_dir, compare_against_dir)
    if not comparisons:
        logger.warning(
            "[%s] discover_comparisons found no pairs in %s vs %s",
            phase_label,
            exec_output_dir,
            compare_against_dir,
        )
        return []

    reports_data: list[dict] = []
    for comparison_name, output_file, expected_file in comparisons:
        logger.info(
            "[%s] Comparing %s vs %s",
            phase_label,
            output_file.name,
            expected_file.parent.name,
        )
        report_path = validation_output_dir / f"{report_prefix}_report_{comparison_name}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)

        custom_config = ValidationConfig()
        custom_config.sql_path = str(code_file_path)
        custom_config.talend_path = str(expected_file)
        custom_config.output_json_path = str(report_path)

        engine = ValidationEngine(config=custom_config, run_path=code_file_path.parent.parent)
        engine.run(
            source_df=load_csv(str(expected_file)),
            target_df=load_csv(str(output_file)),
        )

        if report_path.is_file():
            reports_data.append(json.loads(report_path.read_text(encoding="utf-8")))
            logger.info("[%s] Report saved → %s", phase_label, report_path)
        else:
            logger.warning("[%s] Report not written by engine: %s", phase_label, report_path)

    return reports_data


# ─────────────────────────────────────────────────────────────────────────────
# Correction-agent wrapper
# ─────────────────────────────────────────────────────────────────────────────


class _ValidationOutputWrapper:
    """
    Thin wrapper that gives CorrectionAgent the interface it expects.

    BUG FIXED: the original used type("Issue", (), payload.get("issue", {}))()
    which creates a class whose dict-keys become *class* attributes shared
    across all instances. We now use a proper SimpleNamespace so each wrapper
    gets its own independent issue object.
    """
    def __init__(self, payload: dict) -> None:
        from types import SimpleNamespace

        issue_dict = payload.get("issue") or {}

        if not isinstance(issue_dict, dict):
            issue_dict = {}

        self.issue = SimpleNamespace(
            type=issue_dict.get("type", "runtime"),
            details=issue_dict.get("details", ""),
        )

        self.code_query: str = payload.get("code_query", "")
        self.sql_query: str = payload.get("sql_query", "")

    def get_code(self) -> str:
        return self.code_query or self.sql_query


async def _call_correction_agent(
    *,
    output_system: str,
    input_system: str,
    phase_label: str,
    iteration: int,
    payload: dict,
) -> str:
    """
    Invoke CorrectionAgent with *payload* and return the corrected code string.
    Returns '' on any failure so callers can skip the iteration cleanly.
    """
    correction_agent = CorrectionAgent(
        agent_name=f"{output_system.capitalize()} Correction Agent [{phase_label}]",
        session_id=f"{output_system}_correction_{phase_label}_{int(time.time())}",
        output_system=output_system,
        input_system=input_system,
    )

    wrapper = _ValidationOutputWrapper(payload)

    try:
        result = await correction_agent.runCorrectionAgent(wrapper)
    except Exception as exc:
        logger.error(
            "[%s] CorrectionAgent raised on iteration %d: %s",
            phase_label, iteration, exc,
        )
        return ""

    agent_output = result.get("output") if isinstance(result, dict) else None
    if not agent_output:
        logger.error(
            "[%s] CorrectionAgent returned no output on iteration %d.",
            phase_label, iteration,
        )
        return ""

    corrected_code = (
        getattr(agent_output, "corrected_code", None)
        or getattr(agent_output, "corrected_sql_query", None)
        or getattr(agent_output, "corrected_pyspark_code", None)
        or ""
    )

    if not corrected_code:
        logger.error(
            "[%s] CorrectionAgent returned empty code on iteration %d.",
            phase_label, iteration,
        )

    return corrected_code


# ─────────────────────────────────────────────────────────────────────────────
# Correction loop
# ─────────────────────────────────────────────────────────────────────────────


async def _run_correction_loop(
    *,
    run_path: Path,
    output_system: str,
    input_system: str,
    initial_payload: dict,
    corrected_filename: str,
    phase_label: str,
    test_data_dir: Path,
    compare_against_dir: Path,
    execution_strategy: str,
    phase_output_dir: Path,
    validation_log_path: Path,
    report_prefix: str,
    max_iterations: int = MAX_CORRECTION_ITERATIONS,
) -> tuple[str, dict, list[dict]]:
    """
    Correction loop: up to *max_iterations* attempts of
    CorrectionAgent → write file → execute → compare → narrative.

    All re-execution outputs go directly into *phase_output_dir*
    (simulation_val or java_val) — no per-iteration sub-folders are created.
    Reports overwrite the previous iteration's reports in-place.
    *validation_log_path* is overwritten after every iteration so it always
    reflects the latest state; no _after_correction variant is written.

    Returns
    -------
    best_code : str
        The last corrected code that was successfully written to disk.
        Empty string only if CorrectionAgent returned empty on every iteration.
    final_log : dict
        Envelope {output: {...}, error: 0|1} for the last iteration that
        produced reports, or a failure envelope if none did.
    iterations_history : list[dict]
        One entry per iteration with full detail.

    FILE GUARANTEE
    --------------
    The corrected SQL file (corrected_filename) is written to disk as soon
    as CorrectionAgent returns non-empty code — before re-execution starts.
    If re-execution or comparison fails the file stays on disk unchanged.
    It is never deleted between iterations; only overwritten with a newer
    attempt.
    """
    correction_output_dir = run_path / "output" / "output_correction_agent"
    correction_output_dir.mkdir(parents=True, exist_ok=True)
    corrected_file_path = correction_output_dir / corrected_filename

    current_payload = initial_payload
    best_code: str = (
        initial_payload.get("code_query")
        or initial_payload.get("sql_query")
        or ""
    )
    final_log: dict = {"output": current_payload, "error": 1}
    iterations_history: list[dict] = []

    for iteration in range(1, max_iterations + 1):
        logger.info(
            "[%s] ── Correction iteration %d / %d ──",
            phase_label, iteration, max_iterations,
        )

        # ── 1. Ask CorrectionAgent for a fix ────────────────────────────
        corrected_code = await _call_correction_agent(
            output_system=output_system,
            input_system=input_system,
            phase_label=phase_label,
            iteration=iteration,
            payload=current_payload,
        )

        if not corrected_code:
            iterations_history.append({
                "iteration": iteration,
                "corrected_code": "",
                "corrected_file_written": False,
                "validation_status": "failed",
                "error": "correction_agent_returned_empty_code",
            })
            # Keep current_payload unchanged so the next iteration gets the
            # same context (the agent might succeed on retry).
            continue

        # ── 2. Write corrected file to disk ─────────────────────────────
        # Done BEFORE re-execution so the file always exists on disk after
        # a successful CorrectionAgent call, regardless of what follows.
        # Overwrites the previous iteration's file — no archive copies.
        _write_corrected_file(corrected_file_path, corrected_code, iteration, phase_label)
        best_code = corrected_code

        # ── 3. Re-execute corrected code ─────────────────────────────────
        # Outputs go directly into the stable phase dir (simulation_val /
        # java_val); no per-iteration sub-folder is created.
        try:
            execute_code(
                strategy=execution_strategy,
                run_path=run_path,
                code_path=corrected_file_path,
                output_dir=phase_output_dir,
                test_data_dir=test_data_dir,
                validation_log_path=validation_log_path,
            )
            logger.info(
                "[%s] Re-execution finished → %s (iteration %d)",
                phase_label, phase_output_dir, iteration,
            )

        except Exception as exec_exc:
            logger.error(
                "[%s] Re-execution failed on iteration %d: %s",
                phase_label, iteration, exec_exc,
            )
            fail_payload = _build_failure_payload(corrected_code, output_system, exec_exc)
            iter_log = {"output": fail_payload, "error": 1}
            # Overwrite the phase validation log so it reflects latest state.
            _persist_validation_log(validation_log_path, iter_log)
            iterations_history.append({
                "iteration": iteration,
                "corrected_code": corrected_code,
                "corrected_file_written": True,
                "corrected_file_path": str(corrected_file_path),
                "validation_status": "failed",
                "error": f"re_execution_failed: {exec_exc}",
            })
            current_payload = fail_payload
            final_log = iter_log
            continue

        # ── 4. Compare outputs ────────────────────────────────────────────
        # Reports overwrite previous ones in the same phase dir.
        reports_data = _run_comparisons(
            exec_output_dir=phase_output_dir,
            compare_against_dir=compare_against_dir,
            code_file_path=corrected_file_path,
            validation_output_dir=phase_output_dir,
            report_prefix=report_prefix,
            phase_label=phase_label,
        )

        # ── 5. Generate narrative ─────────────────────────────────────────
        iter_log = _run_narrative(reports_data, corrected_code, output_system)
        # Overwrite the main phase log — no _after_correction variant.
        _persist_validation_log(validation_log_path, iter_log)

        iter_status = _extract_status(iter_log)

        iterations_history.append({
            "iteration": iteration,
            "corrected_code": corrected_code,
            "corrected_file_written": True,
            "corrected_file_path": str(corrected_file_path),
            "validation_status": iter_status,
        })

        final_log = iter_log
        current_payload = iter_log.get("output") or iter_log

        if iter_status.lower() == "success":
            logger.info(
                "[%s] Correction succeeded at iteration %d — file: %s",
                phase_label, iteration, corrected_file_path,
            )
            return best_code, final_log, iterations_history

    logger.warning(
        "[%s] All %d correction iterations exhausted without success.",
        phase_label, max_iterations,
    )
    return best_code, final_log, iterations_history


# ─────────────────────────────────────────────────────────────────────────────
# Single validation phase (execute → compare → narrative → optional correction)
# ─────────────────────────────────────────────────────────────────────────────


async def _run_validation_phase(
    *,
    run_path: Path,
    output_system: str,
    input_system: str,
    cfg: dict,
    code_file_path: Path,
    output_subdir: str,
    compare_against_dir: Path,
    phase_label: str,
    log_filename: str,
    corrected_filename: str,
    report_prefix: str,
) -> dict:
    """
    Execute one full validation phase (execution → comparison → optional correction).

    All outputs (execution results, comparison reports, validation log) live in
    the stable phase directory (simulation_val or java_val).  The correction
    loop re-uses the same directory and overwrites files in-place — no
    per-iteration sub-folders are created.

    Returns
    -------
    dict with keys:
        status : "success" | "failure"
        validation_log : envelope dict
        winning_code : str
        winning_path : Path
        correction_done : bool
        correction_iterations_history : list[dict]
        phase : str
    """
    validation_output_dir = run_path / "output" / "output_validation_agent"
    phase_output_dir = validation_output_dir / output_subdir
    phase_output_dir.mkdir(parents=True, exist_ok=True)

    test_data_dir = run_path / "output" / "output_test_bench_agent" / "DataGenAgent"
    validation_log_path = validation_output_dir / log_filename
    code_query = _read_code_safe(code_file_path)

    logger.info(
        "=== [%s] Starting | output_system=%s | code=%s ===",
        phase_label, output_system, code_file_path,
    )

    # Shared kwargs forwarded to _run_correction_loop so it writes to the
    # same stable dirs as the initial execution.
    correction_loop_kwargs = dict(
        run_path=run_path,
        output_system=output_system,
        input_system=input_system,
        corrected_filename=corrected_filename,
        phase_label=phase_label,
        test_data_dir=test_data_dir,
        compare_against_dir=compare_against_dir,
        execution_strategy=cfg["execution_strategy"],
        phase_output_dir=phase_output_dir,
        validation_log_path=validation_log_path,
        report_prefix=report_prefix,
    )

    # ── Step 1: Execute ──────────────────────────────────────────────────
    try:
        execute_code(
            strategy=cfg["execution_strategy"],
            run_path=run_path,
            code_path=code_file_path,
            output_dir=phase_output_dir,
            test_data_dir=test_data_dir,
            validation_log_path=validation_log_path,
        )
        logger.info("[%s] Execution finished → %s", phase_label, phase_output_dir)

    except Exception as exec_exc:
        logger.error("[%s] Execution failed: %s", phase_label, exec_exc)

        fail_payload = _build_failure_payload(code_query, output_system, exec_exc)
        logger.info("[%s] Execution failed → entering correction loop.", phase_label)

        best_code, corr_log, corr_history = await _run_correction_loop(
            initial_payload=fail_payload,
            **correction_loop_kwargs,
        )

        corrected_status = _extract_status(corr_log)
        corrected_file_path = (
            run_path / "output" / "output_correction_agent" / corrected_filename
        )

        return {
            "status": corrected_status,
            "validation_log": corr_log,
            "winning_code": best_code or code_query,
            "winning_path": (
                corrected_file_path if corrected_file_path.exists() else code_file_path
            ),
            "correction_done": True,
            "correction_iterations_history": corr_history,
            "phase": phase_label,
        }

    # ── Step 2: Compare ──────────────────────────────────────────────────
    reports_data = _run_comparisons(
        exec_output_dir=phase_output_dir,
        compare_against_dir=compare_against_dir,
        code_file_path=code_file_path,
        validation_output_dir=phase_output_dir,
        report_prefix=report_prefix,
        phase_label=phase_label,
    )

    # ── Step 3: Narrative ────────────────────────────────────────────────
    validation_log = _run_narrative(reports_data, code_query, output_system)
    _persist_validation_log(validation_log_path, validation_log)

    phase_status = _extract_status(validation_log)
    logger.info("[%s] Phase status after first execution: %s", phase_label, phase_status)

    # ── Step 4: Correction loop on failure ───────────────────────────────
    if phase_status.lower() != "success":
        logger.info(
            "[%s] Entering correction loop (max %d iterations).",
            phase_label, MAX_CORRECTION_ITERATIONS,
        )

        # Pass the inner payload (not the envelope) to the correction loop.
        initial_payload = validation_log.get("output") or {}
        if not isinstance(initial_payload, dict):
            initial_payload = _build_failure_payload(code_query, output_system, "bad payload")

        best_code, corr_log, corr_history = await _run_correction_loop(
            initial_payload=initial_payload,
            **correction_loop_kwargs,
        )

        corrected_status = _extract_status(corr_log)
        # validation_log_path has already been overwritten inside the loop
        # with the latest result — no separate _after_correction file needed.

        corrected_file_path = (
            run_path / "output" / "output_correction_agent" / corrected_filename
        )
        winning_path = (
            corrected_file_path
            if corrected_file_path.exists()
            else code_file_path
        )

        logger.info(
            "[%s] Correction loop finished | status=%s | winning_path=%s",
            phase_label, corrected_status, winning_path,
        )

        return {
            "status": corrected_status,
            "validation_log": corr_log,
            "winning_code": best_code or code_query,
            "winning_path": winning_path,
            "correction_done": True,
            "correction_iterations_history": corr_history,
            "phase": phase_label,
        }

    # Success on first execution — no correction needed.
    logger.info("[%s] Success without correction.", phase_label)
    return {
        "status": "success",
        "validation_log": validation_log,
        "winning_code": code_query,
        "winning_path": code_file_path,
        "correction_done": False,
        "correction_iterations_history": [],
        "phase": phase_label,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


async def run_validation_agent(
    run_path: Path,
    use_correction: bool = False,
    output_system: str = "sql",
    input_system: str = "talend",
    code_path: Path | PathLike | None = None,
) -> dict:
    """
    Two-phase validation agent.

    Returns an envelope dict compatible with the orchestrator contract:

    {
        "output": {
            "status"      : "success" | "failure",
            "sql_query"   : <winning code>,   # legacy compat
            "code_query"  : <winning code>,
            "output_system": ...,
            "winning_code" : ...,
            "winning_path" : ...,
            "winning_phase": "phase1" | "phase2" | None,
            "phase1" : { status, validation_log, winning_path,
                         correction_done, correction_iterations_history },
            "phase2" : { ... },
            "issue"  : { type, details }  # only on total failure
        },
        "error": 0 | 1
    }
    """
    run_path = Path(run_path)

    initial_code_file = _resolve_code_path(
        run_path=run_path,
        output_system=output_system,
        use_correction=use_correction,
        code_path=code_path,
    )

    cfg = get_output_system_config(output_system)

    validation_output_dir = run_path / "output" / "output_validation_agent"
    validation_output_dir.mkdir(parents=True, exist_ok=True)

    simulation_agent_dir = (
        run_path / "output" / "output_test_bench_agent" / "SimulationAgent"
    )
    java_executor_dir = run_path / "output" / "output_java_execution_agent"

    # ═════════════════════════════════════════════════════════════════════
    # PHASE 1 — Simulation validation
    # ═════════════════════════════════════════════════════════════════════
    logger.info("━━━ PHASE 1: Simulation validation ━━━")

    phase1_result = await _run_validation_phase(
        run_path=run_path,
        output_system=output_system,
        input_system=input_system,
        cfg=cfg,
        code_file_path=initial_code_file,
        output_subdir="simulation_val",
        compare_against_dir=simulation_agent_dir,
        phase_label="phase1_simulation",
        log_filename="validation_log_simulation.json",
        corrected_filename="transformations.sql",
        report_prefix="simulation",
    )

    logger.info(
        "━━━ PHASE 1 done | status=%s | correction_done=%s | winning_path=%s ━━━",
        phase1_result["status"],
        phase1_result["correction_done"],
        phase1_result["winning_path"],
    )

    # ═════════════════════════════════════════════════════════════════════
    # PHASE 2 — Java validation
    # Use phase-1 winner (corrected or original) as input.
    # ═════════════════════════════════════════════════════════════════════
    logger.info("━━━ PHASE 2: Java validation ━━━")

    phase2_input = Path(str(phase1_result["winning_path"]))
    if not phase2_input.exists():
        logger.warning(
            "Phase-1 winning_path does not exist (%s), falling back to initial file.",
            phase2_input,
        )
        phase2_input = initial_code_file

    phase2_result = await _run_validation_phase(
        run_path=run_path,
        output_system=output_system,
        input_system=input_system,
        cfg=cfg,
        code_file_path=phase2_input,
        output_subdir="java_val",
        compare_against_dir=java_executor_dir,
        phase_label="phase2_java",
        log_filename="validation_log_java.json",
        corrected_filename="transformations_java.sql",
        report_prefix="java",
    )

    logger.info(
        "━━━ PHASE 2 done | status=%s | correction_done=%s | winning_path=%s ━━━",
        phase2_result["status"],
        phase2_result["correction_done"],
        phase2_result["winning_path"],
    )

    # ═════════════════════════════════════════════════════════════════════
    # Determine overall winner
    # ═════════════════════════════════════════════════════════════════════
    p1_ok = phase1_result["status"].lower() == "success"
    p2_ok = phase2_result["status"].lower() == "success"

    if p1_ok and p2_ok:
        overall_status = "success"
        winning_phase = "phase2"  # prefer stricter java-validated result
        winning_result = phase2_result
        logger.info("Both phases succeeded — using phase-2 result.")
    elif p2_ok:
        overall_status = "success"
        winning_phase = "phase2"
        winning_result = phase2_result
        logger.info("Phase 2 succeeded — using phase-2 result.")
    elif p1_ok:
        overall_status = "success"
        winning_phase = "phase1"
        winning_result = phase1_result
        logger.info("Phase 1 succeeded — using phase-1 result.")
    else:
        overall_status = "failure"
        winning_phase = None
        winning_result = phase2_result  # best we have
        logger.warning("Both phases failed.")

    winning_code = winning_result["winning_code"]
    winning_path = str(winning_result["winning_path"])

    # ═════════════════════════════════════════════════════════════════════
    # Build combined payload
    # ═════════════════════════════════════════════════════════════════════
    combined_payload: dict = {
        "status": overall_status,
        # Legacy keys expected by orchestrator / InputModel
        "sql_query": winning_code,
        "code_query": winning_code,
        "output_system": output_system,
        # Two-phase details
        "winning_phase": winning_phase,
        "winning_code": winning_code,
        "winning_path": winning_path,
        "phase1": {
            "status": phase1_result["status"],
            "validation_log": phase1_result["validation_log"],
            "winning_path": str(phase1_result["winning_path"]),
            "correction_done": phase1_result["correction_done"],
            "correction_iterations_history": phase1_result["correction_iterations_history"],
        },
        "phase2": {
            "status": phase2_result["status"],
            "validation_log": phase2_result["validation_log"],
            "winning_path": str(phase2_result["winning_path"]),
            "correction_done": phase2_result["correction_done"],
            "correction_iterations_history": phase2_result["correction_iterations_history"],
        },
    }

    # Populate issue on total failure so InputModel / correction can consume it.
    if overall_status != "success":
        fallback_issue = {"type": "runtime", "details": "Both validation phases failed."}
        p2_inner = phase2_result["validation_log"].get("output") or {}
        combined_payload["issue"] = (
            p2_inner.get("issue", fallback_issue)
            if isinstance(p2_inner, dict)
            else fallback_issue
        )

    final_log = {
        "output": combined_payload,
        "error": 0 if overall_status == "success" else 1,
    }

    _persist_validation_log(validation_output_dir / "validation_log.json", final_log)

    logger.info(
        "=== Validation agent finished | overall=%s | winning_phase=%s | winning_path=%s ===",
        overall_status, winning_phase, winning_path,
    )

    return final_log
