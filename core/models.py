"""
Validation Engine – Core Data Models

Status is binary: PASSED or FAILED.
No severity levels. No warnings.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


class Status(str, Enum):
    PASSED  = "PASSED"
    FAILED  = "FAILED"
    SKIPPED = "SKIPPED"   # used when we lack required info (e.g. no PK known)


@dataclass
class CheckResult:
    """Result of one atomic validation check."""
    layer:      str
    check_name: str
    status:     Status
    message:    str
    details:    dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    # If status is SKIPPED, this explains what info we'd need to run the check.
    skipped_reason: str = ""


@dataclass
class LayerResult:
    layer_name:  str
    status:      Status
    checks:      list[CheckResult] = field(default_factory=list)
    duration_ms: float = 0.0

    def add(self, check: CheckResult) -> None:
        self.checks.append(check)
        if check.status == Status.FAILED:
            self.status = Status.FAILED

    @property
    def passed(self) -> bool:
        return self.status == Status.PASSED

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == Status.FAILED]

    @property
    def skipped_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == Status.SKIPPED]


@dataclass
class ColumnTolerance:
    """Per-column floating-point tolerance."""
    column:   str
    absolute: float = 1e-6
    relative: float = 1e-4


@dataclass
class BusinessRule:
    """Custom SQL invariant. Use {source} and {target} as table placeholders."""
    name:       str
    expression: str    # e.g. "SELECT SUM(revenue) FROM {table}"
    tolerance:  float = 0.0


@dataclass
class ValidationConfig:

    
    # Paths
    talend_path: str = ""
    sql_path:    str = ""
    run_id:      str = ""
    source_label: str = "Talend"
    target_label: str = "SQL"

    # Primary key columns — if empty, row-aligned checks are SKIPPED
    # and the engine will tell you what you're missing.
    primary_key: list[str] = field(default_factory=list)

    # Structural layer
    check_column_order: bool = False

    # Business layer
    aggregation_columns: list[str] = field(default_factory=list)
    group_by_columns:    list[str] = field(default_factory=list)
    business_rules:      list[BusinessRule] = field(default_factory=list)
    column_tolerances:   list[ColumnTolerance] = field(default_factory=list)
    default_abs_tolerance: float = 1e-6
    default_rel_tolerance: float = 1e-4

    # Statistical layer
    percentiles:     list[float] = field(default_factory=lambda: [0.50, 0.95, 0.99])
    stat_tolerance:  float = 0.01

    # Output
    output_json_path: str = "validation_report.json"


@dataclass
class ValidationReport:
    run_id:         str
    timestamp:      str
    source_label:   str
    target_label:   str
    overall_status: Status
    total_duration_ms: float
    layers:   list[LayerResult]      = field(default_factory=list)
    summary:  dict[str, Any]         = field(default_factory=dict)
    ai_narrative: str                = ""

    # Checks that were skipped due to missing configuration
    skipped_notices: list[str]       = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    @property
    def passed(self) -> bool:
        return self.overall_status == Status.PASSED

    @classmethod
    def bootstrap(cls, config: ValidationConfig) -> "ValidationReport":
        return cls(
            run_id=config.run_id or datetime.utcnow().strftime("%Y%m%d_%H%M%S"),
            timestamp=datetime.utcnow().isoformat(),
            source_label=config.source_label,
            target_label=config.target_label,
            overall_status=Status.PASSED,
            total_duration_ms=0.0,
        )