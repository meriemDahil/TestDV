#!/usr/bin/env python3
"""
run_test_bench_agent.py
~~~~~~~~~~~~~~~~~~~~~~~
Orchestrates the two-step test-bench pipeline:

  Step 1 — DataGenAgent
    Input  : talend spec
    Output : model_data + input_data  (saved under DataGenAgent/<table>/)

  Step 2 — SimulationAgent
    Input  : spec + input_data rows from Step 1
    Output : model_data + expected_output  (saved under SimulationAgent/<table>/)
"""

import csv
import json
from pathlib import Path
from typing import Any, List, Optional, Union

from pydantic import BaseModel

from migration_factory_backend.config.openai_config import init_openai
from migration_factory_backend.config.test_bench_agent_conf import DEFAULT_ROWS
from migration_factory_backend.modules.test_bench.data_gen_agent import (
    DataGenAgent,
    DataGenTableResult,
)
from migration_factory_backend.modules.test_bench.simulation_agent import SimulationAgent
from migration_factory_backend.utils.common_utils import extract_used_tables_names
from migration_factory_backend.utils.logger import initLogger

logger = initLogger("TestBenchPipeline")


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_serializable(obj: Any) -> Any:
    """Recursively convert Pydantic models to JSON-serialisable structures."""
    if isinstance(obj, BaseModel):
        return _to_serializable(
            obj.model_dump() if hasattr(obj, "model_dump") else obj.dict()
        )
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(v) for v in obj]
    return obj


def _save_csv(
    agent_name: str,
    table_name: str,
    suffix: str,
    data: List[Any],
    output_dir: Union[str, Path],
) -> None:
    """
    Save a list of dicts to:
      <output_dir>/<agent_name>/<table_name>/<suffix>.csv
    """
    if not data:
        logger.warning("[%s][%s] No data for '%s', skipping.", agent_name, table_name, suffix)
        return

    serializable = _to_serializable(data)
    dest = Path(output_dir) / agent_name / table_name
    dest.mkdir(parents=True, exist_ok=True)
    file_path = dest / f"{suffix}.csv"

    try:
        keys = serializable[0].keys()
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(serializable)
        logger.info("[%s][%s] %s → %s (%d rows)", agent_name, table_name, suffix, file_path, len(serializable))
        print(f"  [OK] {file_path}")
    except Exception:
        logger.error("[%s][%s] Failed saving '%s'", agent_name, table_name, suffix, exc_info=True)
        raise


# ── validation ────────────────────────────────────────────────────────────────

def _validate_data_gen(output, used_tables: List[str]) -> int:
    if not output or not output.tables:
        logger.error("DataGenAgent produced no tables.")
        return 1
    generated = {t.table_name for t in output.tables}
    missing = set(used_tables) - generated
    if missing:
        logger.error("DataGenAgent missing tables: %s", missing)
        return 1
    for t in output.tables:
        if not t.model_data:
            logger.error("DataGenAgent: model_data empty for '%s'", t.table_name)
            return 1
        if not t.input_data:
            logger.error("DataGenAgent: input_data empty for '%s'", t.table_name)
            return 1
    logger.info("DataGenAgent validation OK.")
    return 0


def _validate_simulation(output, used_tables: List[str]) -> int:
    if not output or not output.tables:
        logger.error("SimulationAgent produced no tables.")
        return 1
    generated = {t.table_name for t in output.tables}
    missing = set(used_tables) - generated
    if missing:
        logger.error("SimulationAgent missing tables: %s", missing)
        return 1
    for t in output.tables:
        if not t.expected_output:
            logger.error("SimulationAgent: expected_output empty for '%s'", t.table_name)
            return 1
    logger.info("SimulationAgent validation OK.")
    return 0


# ── main pipeline ─────────────────────────────────────────────────────────────

async def run_test_bench_agent(
    pydantic_input: List[BaseModel],
    output_path: Union[str, Path],
    rows: int = DEFAULT_ROWS,
    used_tables: Optional[List[str]] = None,
) -> int:
    """
    Returns 0 on success, 1 on any failure.

    Output structure:
      <output_path>/
        DataGenAgent/
          <table_name>/
            model_data.csv
            input_data.csv
        SimulationAgent/
          <table_name>/
            model_data.csv
            expected_output.csv
    """
    output_path = Path(output_path)
    print(f"\n{'='*54}\n  TEST BENCH PIPELINE\n{'='*54}")
    print(f"  Rows requested : {rows}")

    logger.info("Starting TestBench pipeline | output=%s | rows=%d", output_path, rows)

    init_openai()

    serialized_input = _to_serializable(pydantic_input)

    if used_tables is None:
        used_tables = extract_used_tables_names(serialized_input)
        logger.info("used_tables auto-extracted: %s", used_tables)

    print(f"  Tables         : {used_tables}\n{'='*54}\n")

    # ── Step 1: DataGenAgent ──────────────────────────────────────────────────
    print("[ 1/2 ] DataGenAgent — generating schema + input rows…")
    logger.info("Launching DataGenAgent for tables: %s", used_tables)

    try:
        data_gen_agent = DataGenAgent()
        data_gen_output = await data_gen_agent.run(
            json_input={"tables": serialized_input},
            num_rows=rows,
            used_tables=used_tables,
        )
    except Exception:
        logger.error("DataGenAgent raised an exception.", exc_info=True)
        return 1

    if _validate_data_gen(data_gen_output, used_tables) != 0:
        print("❌ DataGenAgent validation failed — see logs.")
        return 1

    print("\n  → Saving DataGenAgent outputs…")
    for table in data_gen_output.tables:
        _save_csv("DataGenAgent", table.table_name, "model_data", table.model_data, output_path)
        _save_csv("DataGenAgent", table.table_name, "input_data", table.input_data, output_path)

    # ── Step 2: SimulationAgent ───────────────────────────────────────────────
    print("\n[ 2/2 ] SimulationAgent — simulating transformations…")
    logger.info("Launching SimulationAgent for %d tables.", len(data_gen_output.tables))

    try:
        simulation_agent = SimulationAgent()
        simulation_output = await simulation_agent.run(
            spec={"tables": serialized_input},
            data_gen_output_tables=data_gen_output.tables,
        )
    except Exception:
        logger.error("SimulationAgent raised an exception.", exc_info=True)
        return 1

    if _validate_simulation(simulation_output, used_tables) != 0:
        print("❌ SimulationAgent validation failed — see logs.")
        return 1

    print("\n  → Saving SimulationAgent outputs…")
    for table in simulation_output.tables:
        _save_csv("SimulationAgent", table.table_name, "model_data", table.model_data, output_path)
        _save_csv("SimulationAgent", table.table_name, "expected_output", table.expected_output, output_path)

    print(f"\n{'='*54}")
    print("  ✅ TestBench pipeline complete.")
    print(f"{'='*54}\n")
    logger.info("TestBench pipeline finished successfully.")
    return 0
