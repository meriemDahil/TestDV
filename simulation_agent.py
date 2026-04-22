import json
import asyncio
from typing import List, Optional

from agents import AgentOutputSchema
from openai import APITimeoutError
from pydantic import BaseModel, Field

from migration_factory_backend.services.general_agent import GeneralAgent
from migration_factory_backend.config.test_bench_agent_conf import DEFAULT_ROWS
from migration_factory_backend.utils.logger import initLogger
from migration_factory_backend.modules.test_bench.data_gen_agent import (
    ColumnSchema,
    DataGenTableResult,
)


# ── IO models ────────────────────────────────────────────────────────────────

class SimulationTableResult(BaseModel):
    table_name: str
    model_data: List[ColumnSchema] = Field(default_factory=list)
    expected_output: List[dict] = Field(default_factory=list)


class SimulationAgentOutput(BaseModel):
    tables: List[SimulationTableResult] = Field(default_factory=list)


# ── Agent ─────────────────────────────────────────────────────────────────────

AGENT_NAME = "SimulationAgent"

INSTRUCTIONS = """
You are a SQL transformation simulator for PostgreSQL.

You receive:
  - The original data migration specification (transformations, mappings, filters…)
  - A table's model_data (column schema)
  - A table's input_data (concrete rows that have already been generated)

Your job is to simulate exactly what the specified SQL transformations would produce
when applied to those exact input rows. Return the resulting rows as expected_output.

Rules:
- Apply EVERY transformation rule from the spec to EVERY input row.
- If a transformation produces a new column name, use the output column name.
- If a row would be filtered out by a WHERE clause, omit it from expected_output.
- If a column is derived (concatenation, cast, arithmetic…), compute the value.
- DATES      → 'YYYY-MM-DD'
- TIMESTAMPS → 'YYYY-MM-DD HH:MM:SS'
- BOOLEANS   → lowercase true / false
- NUMERICS   → dot decimal separator
- Output STRICT JSON only — no markdown, no commentary.
"""


class SimulationAgent(GeneralAgent):
    logger = initLogger(AGENT_NAME)

    def __init__(self) -> None:
        super().__init__(
            agent_name=AGENT_NAME,
            instructions=INSTRUCTIONS,
            io_model=AgentOutputSchema(SimulationAgentOutput, strict_json_schema=False),
        )
        self.agent.tools = []

    # ── prompt ───────────────────────────────────────────────────────────────

    def build_prompt(
        self,
        spec: dict,
        table_result: DataGenTableResult,
    ) -> str:
        spec_json = json.dumps(spec, indent=2, ensure_ascii=False)
        model_data_json = json.dumps(
            [col.model_dump() for col in table_result.model_data],
            indent=2,
            ensure_ascii=False,
        )
        input_data_json = json.dumps(table_result.input_data, indent=2, ensure_ascii=False)

        return f"""
TRANSFORMATION SPECIFICATION (JSON):
{spec_json}

TABLE: {table_result.table_name}

model_data (column schema):
{model_data_json}

input_data (rows to transform — do not invent new rows):
{input_data_json}

TASK
----
Simulate the SQL transformation rules from the spec applied to the input_data rows above.
Return expected_output: the rows that the SQL query would produce.

OUTPUT FORMAT (strict JSON, no markdown fences):
{{
  "tables": [
    {{
      "table_name": "{table_result.table_name}",
      "model_data": [ /* same schema, output column names */ ],
      "expected_output": [
        {{ /* transformed row 1 */ }},
        {{ /* transformed row 2 */ }}
      ]
    }}
  ]
}}
""".strip()

    # ── response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, content) -> dict:
        if isinstance(content, dict):
            return content
        if hasattr(content, "model_dump"):
            return content.model_dump()

        content = str(content).strip()
        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            ).strip()

        first, last = content.find("{"), content.rfind("}")
        if first == -1 or last == -1:
            raise ValueError(f"No JSON found in SimulationAgent output:\n{content}")
        content = content[first : last + 1]

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from SimulationAgent: {e}\n{content}")

    # ── main entry point ──────────────────────────────────────────────────────

    async def run(
        self,
        spec: dict,
        data_gen_output_tables: List[DataGenTableResult],
    ) -> SimulationAgentOutput:
        all_tables: List[SimulationTableResult] = []
        max_retries = 3

        for table_result in data_gen_output_tables:
            table_name = table_result.table_name
            prompt = self.build_prompt(spec, table_result)
            last_error = None

            for attempt in range(max_retries):
                try:
                    raw_output = await self.runAgent(prompt)
                    parsed = self._parse_response(raw_output)

                    for item in parsed.get("tables", []):
                        if not isinstance(item, dict):
                            raise ValueError("Each table entry must be a JSON object.")

                        expected_output = item.get("expected_output", [])
                        if not expected_output:
                            raise ValueError(
                                f"expected_output is empty for table '{item.get('table_name')}'"
                            )

                        model_data_raw = item.get("model_data", [])

                        all_tables.append(
                            SimulationTableResult(
                                table_name=item["table_name"],
                                model_data=[ColumnSchema(**col) for col in model_data_raw],
                                expected_output=expected_output,
                            )
                        )
                    break  # success

                except APITimeoutError as e:
                    last_error = e
                    wait = 5 * (attempt + 1)
                    self.logger.warning(
                        "[%s] Timeout attempt %d/%d for table '%s', retrying in %ds…",
                        AGENT_NAME, attempt + 1, max_retries, table_name, wait,
                    )
                    await asyncio.sleep(wait)

                except Exception as e:
                    raise RuntimeError(f"[{AGENT_NAME}] Error on table '{table_name}': {e}")
            else:
                raise RuntimeError(
                    f"[{AGENT_NAME}] Timed out after {max_retries} attempts "
                    f"on table '{table_name}'. Last error: {last_error}"
                )

        return SimulationAgentOutput(tables=all_tables)
