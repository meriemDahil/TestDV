import json
import asyncio
from typing import List, Optional

from agents import AgentOutputSchema
from openai import APITimeoutError
from pydantic import BaseModel, Field

from migration_factory_backend.services.general_agent import GeneralAgent
from migration_factory_backend.config.test_bench_agent_conf import DEFAULT_ROWS
from migration_factory_backend.utils.logger import initLogger


# ── IO models ────────────────────────────────────────────────────────────────

class ColumnSchema(BaseModel):
    column: str
    type: str
    constraint: str = ""


class DataGenTableResult(BaseModel):
    table_name: str
    model_data: List[ColumnSchema] = Field(default_factory=list)
    input_data: List[dict] = Field(default_factory=list)


class DataGenAgentOutput(BaseModel):
    tables: List[DataGenTableResult] = Field(default_factory=list)


# ── Agent ─────────────────────────────────────────────────────────────────────

AGENT_NAME = "DataGenAgent"

INSTRUCTIONS = """
You are a PostgreSQL test-data generator.
Given a table specification (column names, types, constraints), you produce:
  - model_data : the column schema (name, type, constraint)
  - input_data : realistic, constraint-respecting rows

Rules:
- DATES      → 'YYYY-MM-DD'
- TIMESTAMPS → 'YYYY-MM-DD HH:MM:SS'
- BOOLEANS   → lowercase true / false
- NUMERICS   → dot decimal separator
- PRIMARY KEYS must be unique across all rows.
- Foreign-key columns must reference values that exist in the referenced table
  (use the same seed values you generated for that table in this call).
- Output STRICT JSON only — no markdown, no commentary.
"""


class DataGenAgent(GeneralAgent):
    logger = initLogger(AGENT_NAME)

    def __init__(self) -> None:
        super().__init__(
            agent_name=AGENT_NAME,
            instructions=INSTRUCTIONS,
            io_model=AgentOutputSchema(DataGenAgentOutput, strict_json_schema=False),
        )
        self.agent.tools = []

    # ── prompt ───────────────────────────────────────────────────────────────

    def build_prompt(
        self,
        json_data: dict,
        num_rows: int,
        table_name: str,
    ) -> str:
        spec = json.dumps(json_data, indent=2, ensure_ascii=False)
        return f"""
INPUT SPECIFICATION (JSON):
{spec}

TARGET TABLE : {table_name}
ROWS REQUIRED: {num_rows}

TASK
----
1. Produce model_data: the column schema for table "{table_name}".
2. Produce input_data: exactly {num_rows} realistic rows for table "{table_name}".
   - Respect all type and constraint rules above.
   - Do NOT apply any transformations — raw source values only.

OUTPUT FORMAT (strict JSON, no markdown fences):
{{
  "tables": [
    {{
      "table_name": "{table_name}",
      "model_data": [
        {{"column": "id",   "type": "SERIAL", "constraint": "PRIMARY KEY"}},
        {{"column": "name", "type": "VARCHAR(100)", "constraint": "NOT NULL"}}
      ],
      "input_data": [
        {{"id": 1, "name": "Alice"}},
        {{"id": 2, "name": "Bob"}}
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
            raise ValueError(f"No JSON found in DataGenAgent output:\n{content}")
        content = content[first : last + 1]

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from DataGenAgent: {e}\n{content}")

    # ── main entry point ──────────────────────────────────────────────────────

    async def run(
        self,
        json_input: dict,
        num_rows: int = DEFAULT_ROWS,
        used_tables: Optional[List[str]] = None,
    ) -> DataGenAgentOutput:
        all_tables: List[DataGenTableResult] = []
        max_retries = 3

        for table_name in (used_tables or []):
            prompt = self.build_prompt(json_input, num_rows, table_name)
            last_error = None

            for attempt in range(max_retries):
                try:
                    raw_output = await self.runAgent(prompt)
                    parsed = self._parse_response(raw_output)

                    for item in parsed.get("tables", []):
                        if not isinstance(item, dict):
                            raise ValueError("Each table entry must be a JSON object.")

                        model_data_raw = item.get("model_data", [])
                        if not model_data_raw:
                            raise ValueError(
                                f"model_data is empty for table '{item.get('table_name')}'"
                            )

                        input_data = item.get("input_data", [])
                        if not input_data:
                            raise ValueError(
                                f"input_data is empty for table '{item.get('table_name')}'"
                            )

                        all_tables.append(
                            DataGenTableResult(
                                table_name=item["table_name"],
                                model_data=[ColumnSchema(**col) for col in model_data_raw],
                                input_data=input_data,
                            )
                        )
                    break  # success for this table

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

        return DataGenAgentOutput(tables=all_tables)
