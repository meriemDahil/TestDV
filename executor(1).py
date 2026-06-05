"""
pipeline/executor.py
---------------------
SQLExecutor — the thin orchestrator.

Wires InferredCSVLoader + SQLRunner + OutputManager into a single
pipeline. No DDL schema files are required — table structure is
inferred directly from CSV headers.

Every dependency is injected so the executor is fully testable:
  - Pass a SQLite engine  → no PostgreSQL needed
  - Pass a mock validator → no real SQL checking
  - Pass a temp output_dir → no real files written

Usage
-----
from pipeline.models   import make_postgres_engine
from pipeline.executor import SQLExecutor

engine = make_postgres_engine(user="...", password="...")

executor = SQLExecutor.from_paths(
    engine   = engine,
    sql_path = Path("sql_scripts/transformations.sql"),
    data_dir = Path("data"),
)

result = executor.execute()
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger
from sqlalchemy import Engine

from pipeline.models              import ExecutionResult
from pipeline.sql_runner          import SQLRunner
from pipeline.output_manager      import OutputManager
from pipeline.inferred_csv_loader import InferredCSVLoader


class SQLExecutor:
    """
    Orchestrates the SQL transformation pipeline without DDL schema files.

    Parameters
    ----------
    inferred_loader: InferredCSVLoader — scans CSVs, drops stale output
                                         tables, loads inputs into DB
    sql_runner:      SQLRunner         — validates + executes SQL script
    output_manager:  OutputManager     — saves CSVs + persists to DB
    sql_path:        Path              — the transformation SQL file
    data_dir:        Path              — directory containing input CSVs
    """

    def __init__(
        self,
        inferred_loader: InferredCSVLoader,
        sql_runner:      SQLRunner,
        output_manager:  OutputManager,
        sql_path:        Path,
        data_dir:        Path,
    ):
        self._inferred_loader = inferred_loader
        self._sql_runner      = sql_runner
        self._output_manager  = output_manager
        self.sql_path         = Path(sql_path)
        self.data_dir         = Path(data_dir)

    # ── Factory ───────────────────────────────────────────────────

    @classmethod
    def from_paths(
        cls,
        engine:            Engine,
        sql_path:          Path,
        data_dir:          Path,
        output_dir:        Path | None = None,
        issue_report_path: Path | None = None,
        sql_validator=     None,
    ) -> "SQLExecutor":
        """
        Build a fully wired SQLExecutor from paths and an engine.
        No DDL schema files required.

        Parameters
        ----------
        engine:
            SQLAlchemy Engine (PostgreSQL or SQLite for tests).
        sql_path:
            Path to the SQL transformation script.
        data_dir:
            Directory containing input CSV files.
        output_dir:
            Where to write output CSVs. Defaults to data_dir.
        issue_report_path:
            Where to write the validation/error JSON report.
            Defaults to data_dir/validation_report.json.
        sql_validator:
            Optional override for the SQL validator callable.
        """
        data_dir   = Path(data_dir)
        output_dir = Path(output_dir) if output_dir else data_dir

        return cls(
            inferred_loader = InferredCSVLoader(engine),
            sql_runner      = SQLRunner(
                engine            = engine,
                sql_validator     = sql_validator,
                issue_report_path = issue_report_path or data_dir / "validation_report.json",
            ),
            output_manager  = OutputManager(engine, output_dir=output_dir),
            sql_path        = sql_path,
            data_dir        = data_dir,
        )

    # ── Main entry point ──────────────────────────────────────────

    def execute(
        self,
        persist_to_db: bool = True,
        preview:       bool = True,
    ) -> ExecutionResult:
        """
        Run the full pipeline end to end:

          1. Drop stale output tables (tables created by the SQL script)
          2. Load all CSVs from data_dir into the database
          3. Validate + execute the SQL transformation script
          4. Preview results (optional)
          5. Save output CSVs + persist to DB (optional)

        Parameters
        ----------
        persist_to_db:
            Write output DataFrames back to PostgreSQL. Default True.
        preview:
            Print a console preview of output tables. Default True.

        Returns
        -------
        ExecutionResult
            Contains an empty schemas list and all output DataFrames.
        """
        _banner("PostgreSQL SQL EXECUTOR")

        # ── Steps 1-2: prepare the database ───────────────────────
        self._inferred_loader.load(self.data_dir, sql_path=self.sql_path)

        # ── Step 3: validate + execute the SQL ────────────────────
        output_tables = self._sql_runner.run(self.sql_path)

        # ── Steps 4-5: present and persist results ─────────────────
        if preview:
            self._output_manager.preview(output_tables)

        self._output_manager.save_all(
            output_tables,
            persist_to_db=persist_to_db,
        )

        _banner("DONE")

        return ExecutionResult(schemas=[], output_tables=output_tables)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _banner(text: str) -> None:
    logger.info("=" * 70)
    logger.info(f"  {text}")
    logger.info("=" * 70)













"""
run_validation_agent.py
=======================

Two-phase validation pipeline:

PHASE 1 — Simulation validation
  1. Execute code against DataGenAgent test data
  2. Store results in  output_validation_agent/simulation_val/
  3. Compare simulation_val  vs  output_test_bench_agent/SimulationAgent/
  4. Generate per-pair reports + validation_log_simulation.json
  5. On failure → correction loop (max MAX_CORRECTION_ITERATIONS = 5)
     • corrected file persisted as output_correction_agent/transformations.sql
     • each iteration re-executes and re-compares before advancing
     • archive copy written per iteration (never overwritten)

PHASE 2 — Java validation
  1. Execute best available code from phase 1 (corrected or original)
     against DataGenAgent test data
  2. Store results in  output_validation_agent/java_val/
  3. Compare java_val  vs  output_java_execution_agent/
  4. Generate per-pair reports + validation_log_java.json
  5. On failure → correction loop (max MAX_CORRECTION_ITERATIONS = 5)
     • corrected file persisted as output_correction_agent/transformations_java.sql

RESULT
  • If either phase succeeds the orchestrator receives "success" with
    winning_code / winning_path pointing to the successful SQL.
  • Phase 2 winner is preferred over phase 1 when both succeed (stricter).
  • Every intermediate file is kept; nothing is silently discarded.

FILE GENERATION GUARANTEES
  • transformations.sql  is written as soon as CorrectionAgent returns
    non-empty code in phase 1, before re-execution.  It is never deleted;
    each iteration overwrites it with the latest attempt, and an immutable
    archive copy is saved separately.
  • transformations_java.sql  follows the same guarantee for phase 2.
  • If CorrectionAgent returns empty code the previous file on disk is
    kept intact and the iteration is logged as failed.
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
    {output: {...}, error: N}.  Returns 'failure' when absent.
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
    Write *code* to *path* (the canonical corrected-file location) and also
    save an immutable archive copy so earlier iterations are never lost.

    This is the single place that writes correction SQL files.  Calling it
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

    archive_dir = path.parent / f"archive_{phase_label}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{path.stem}_iter{iteration}{path.suffix}"
    archive_path.write_text(code, encoding="utf-8")
    logger.info(
        "[%s] Archive copy written → %s",
        phase_label,
        archive_path,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Narrative helper  (re-used in both the phase runner and the correction loop)
# ─────────────────────────────────────────────────────────────────────────────


def _run_narrative(
    reports_data: list[dict],
    code_query: str,
    output_system: str,
) -> dict:
    """
    Feed *reports_data* through NarrativeService and return a normalised
    envelope  {output: {...}, error: 0|1}.

    BUG FIXED: previously the error flag was read back from the narrative
    result *after* the payload had been extracted from it, always defaulting
    to 0.  Now the error flag is derived from the payload status directly so
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
# Comparison helper  (re-used in both the phase runner and the correction loop)
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

    BUG FIXED: the original used  type("Issue", (), payload.get("issue", {}))()
    which creates a class whose dict-keys become *class* attributes shared
    across all instances.  We now use a proper SimpleNamespace so each wrapper
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
    max_iterations: int = MAX_CORRECTION_ITERATIONS,
) -> tuple[str, dict, list[dict]]:
    """
    Correction loop: up to *max_iterations* attempts of
        CorrectionAgent → write file → execute → compare → narrative.

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
    attempt.  An immutable archive copy is always saved alongside it.
    """
    correction_output_dir = run_path / "output" / "output_correction_agent"
    correction_output_dir.mkdir(parents=True, exist_ok=True)
    corrected_file_path = correction_output_dir / corrected_filename

    validation_output_dir = run_path / "output" / "output_validation_agent"

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
        # This is done BEFORE re-execution so the file always exists after
        # a successful CorrectionAgent call, regardless of what follows.
        _write_corrected_file(corrected_file_path, corrected_code, iteration, phase_label)
        best_code = corrected_code

        # ── 3. Re-execute corrected code ─────────────────────────────────
        re_exec_dir = validation_output_dir / f"{phase_label}_reexec_iter{iteration}"
        re_exec_dir.mkdir(parents=True, exist_ok=True)
        re_exec_log_path = re_exec_dir / "validation_log.json"

        try:
            execute_code(
                strategy=execution_strategy,
                run_path=run_path,
                code_path=corrected_file_path,
                output_dir=re_exec_dir,
                test_data_dir=test_data_dir,
                validation_log_path=re_exec_log_path,
            )
            logger.info(
                "[%s] Re-execution finished → %s (iteration %d)",
                phase_label, re_exec_dir, iteration,
            )
        except Exception as exec_exc:
            logger.error(
                "[%s] Re-execution failed on iteration %d: %s",
                phase_label, iteration, exec_exc,
            )
            fail_payload = _build_failure_payload(corrected_code, output_system, exec_exc)
            iter_log = {"output": fail_payload, "error": 1}
            _persist_validation_log(re_exec_log_path, iter_log)
            iterations_history.append({
                "iteration": iteration,
                "corrected_code": corrected_code,
                "corrected_file_written": True,
                "corrected_file_path": str(corrected_file_path),
                "validation_status": "failed",
                "error": f"re_execution_failed: {exec_exc}",
            })
            # Update payload so next iteration's correction sees the execution error.
            current_payload = fail_payload
            final_log = iter_log
            continue

        # ── 4. Compare outputs ────────────────────────────────────────────
        reports_data = _run_comparisons(
            exec_output_dir=re_exec_dir,
            compare_against_dir=compare_against_dir,
            code_file_path=corrected_file_path,
            validation_output_dir=re_exec_dir,   # reports go inside the iter dir
            report_prefix=f"{phase_label}_iter{iteration}",
            phase_label=phase_label,
        )

        # ── 5. Generate narrative ─────────────────────────────────────────
        iter_log = _run_narrative(reports_data, corrected_code, output_system)
        _persist_validation_log(re_exec_log_path, iter_log)

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
# Single validation phase  (execute → compare → narrative → optional correction)
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

    Returns
    -------
    dict with keys:
        status                        : "success" | "failure"
        validation_log                : envelope dict
        winning_code                  : str
        winning_path                  : Path
        correction_done               : bool
        correction_iterations_history : list[dict]
        phase                         : str
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
        fail_log = {"output": fail_payload, "error": 1}
        _persist_validation_log(validation_log_path, fail_log)
        return {
            "status": "failure",
            "validation_log": fail_log,
            "winning_code": code_query,
            "winning_path": code_file_path,
            "correction_done": False,
            "correction_iterations_history": [],
            "phase": phase_label,
        }

    # ── Step 2: Compare ──────────────────────────────────────────────────
    reports_data = _run_comparisons(
        exec_output_dir=phase_output_dir,
        compare_against_dir=compare_against_dir,
        code_file_path=code_file_path,
        validation_output_dir=validation_output_dir,
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
            run_path=run_path,
            output_system=output_system,
            input_system=input_system,
            initial_payload=initial_payload,
            corrected_filename=corrected_filename,
            phase_label=phase_label,
            test_data_dir=test_data_dir,
            compare_against_dir=compare_against_dir,
            execution_strategy=cfg["execution_strategy"],
        )

        corrected_status = _extract_status(corr_log)

        # Persist the final post-correction validation log alongside the
        # original phase log so both are always on disk.
        corrected_log_path = (
            validation_output_dir
            / f"{log_filename.replace('.json', '')}_after_correction.json"
        )
        _persist_validation_log(corrected_log_path, corr_log)

        # Resolve where the winning file lives.
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
                "status"       : "success" | "failure",
                "sql_query"    : <winning code>,   # legacy compat
                "code_query"   : <winning code>,
                "output_system": ...,
                "winning_code" : ...,
                "winning_path" : ...,
                "winning_phase": "phase1" | "phase2" | None,
                "phase1"       : { status, validation_log, winning_path,
                                   correction_done, correction_iterations_history },
                "phase2"       : { ... },
                "issue"        : { type, details }  # only on total failure
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
        winning_phase = "phase2"   # prefer stricter java-validated result
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
        winning_result = phase2_result   # best we have
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
