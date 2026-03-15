"""
Layer 5 – Report Generation

Produces:
  1. Structured JSON report (deterministic, machine-readable)
  2. AI narrative via Claude API (human-readable summary)
     — The AI only reads the finished report and writes prose.
       It never performs any validation logic itself.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

from core.models import ValidationReport, LayerResult, Status


# Summary builder

def build_summary(report: ValidationReport) -> dict:
    total = passed = failed = skipped = 0
    failures: list[dict] = []
    skipped_notices: list[str] = []
    layer_statuses: dict[str, str] = {}

    for layer in report.layers:
        layer_statuses[layer.layer_name] = layer.status.value
        for check in layer.checks:
            total += 1
            if check.status == Status.PASSED:
                passed += 1
            elif check.status == Status.FAILED:
                failed += 1
                failures.append({
                    "layer": check.layer,
                    "check": check.check_name,
                    "message": check.message,
                })
            elif check.status == Status.SKIPPED:
                skipped += 1
                skipped_notices.append({
                    "layer": check.layer,
                    "check": check.check_name,
                    "message": check.message,
                    "to_enable": check.skipped_reason,
                })

    return {
        "total_checks": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": round(passed / max(total - skipped, 1), 4),
        "equivalent": report.overall_status == Status.PASSED,
        "failures": failures,
        "skipped_checks": skipped_notices,
        "layer_statuses": layer_statuses,
    }


def finalise_status(report: ValidationReport) -> None:
    for layer in report.layers:
        if layer.status == Status.FAILED:
            report.overall_status = Status.FAILED
            return


def write_json_report(report: ValidationReport, path: str) -> None:
    report.save(path)
    print(f"[report] JSON written → {path}")


# AI narrative

_SYSTEM = """
You are a senior data engineer writing a concise validation summary for a client migration report.
You receive a JSON validation report produced by a deterministic Python engine.

Rules:
- Never invent numbers — only reference what is in the JSON.
- Max 350 words.
- Structure: (1) Overall verdict, (2) Layer-by-layer summary, (3) Skipped checks notice, (4) Action items.
- If checks were skipped due to missing configuration, list them and explain what config is needed.
- If all checks passed, confirm equivalence confidently.
- End with a single-sentence recommendation.
"""


def generate_ai_narrative(report: ValidationReport, api_key: str = "") -> str:
    compact = {
        "run_id": report.run_id,
        "overall_status": report.overall_status.value,
        "source": report.source_label,
        "target": report.target_label,
        "summary": report.summary,
    }
    prompt = "Write the narrative for this validation report:\n\n" + json.dumps(compact, indent=2, default=str)

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["content"][0]["text"]
    except urllib.error.HTTPError as e:
        return f"[AI narrative unavailable — HTTP {e.code}: {e.read().decode()[:200]}]"
    except Exception as e:
        return f"[AI narrative unavailable — {e}]"


# Console printer

def print_console_summary(report: ValidationReport) -> None:
    sep = "─" * 60
    icon = "✓" if report.passed else "✗"
    s = report.summary

    print(f"\n{sep}")
    print(f"  VALIDATION REPORT  [{report.run_id}]")
    print(f"  {report.source_label}  →  {report.target_label}")
    print(sep)
    print(f"  Overall   : {icon} {report.overall_status.value}")
    print(f"  Duration  : {report.total_duration_ms:.1f} ms")
    print(f"  Checks    : {s.get('passed',0)} passed / "
          f"{s.get('failed',0)} failed / "
          f"{s.get('skipped',0)} skipped "
          f"(pass rate {s.get('pass_rate',0):.1%})")
    print(sep)

    for layer in report.layers:
        icon_l = "✓" if layer.status == Status.PASSED else "✗"
        print(f"  {icon_l} {layer.layer_name:<28}  {layer.status.value:<8}  {layer.duration_ms:.1f}ms")

    # Print skipped check notices
    skipped = s.get("skipped_checks", [])
    if skipped:
        print(sep)
        print("  SKIPPED CHECKS — what you need to enable them:")
        for sk in skipped:
            print(f"\n  • [{sk['layer']}] {sk['check']}")
            print(f"    {sk['message']}")
            if sk.get("to_enable"):
                print(f"    → To enable: {sk['to_enable']}")

    # Print failures
    failures = s.get("failures", [])
    if failures:
        print(sep)
        print("  FAILURES:")
        for f in failures:
            print(f"  ✗ [{f['layer']}] {f['check']}: {f['message']}")

    if report.ai_narrative:
        print(sep)
        print("  AI NARRATIVE\n")
        for line in report.ai_narrative.splitlines():
            print(f"  {line}")

    print(f"\n{sep}\n")