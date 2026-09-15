from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .config import CampaignConfig
from .models import ParseResult, Task


def _reported_map(result: ParseResult) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for row in result.runtime_contract:
        if result.final_cycle_source and row.get("source") != result.final_cycle_source:
            continue
        out.setdefault(str(row.get("field")), []).append(row.get("reported_value"))
    return out


def evaluate_contract(config: CampaignConfig, task: Task, result: ParseResult, *, exit_code: int | None, expected_final_cycle: int) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    def add(name: str, passed: bool, observed: Any = None, expected: Any = None, severity: str = "ERROR"):
        checks.append({"name": name, "passed": bool(passed), "observed": observed, "expected": expected, "severity": severity})
    add("engine_exit_code", exit_code == 0, exit_code, 0)
    require_file = bool(config.get("contracts.require_result_file", True))
    add("result_file_present", bool(result.source_files) or not require_file, len(result.source_files), ">=1" if require_file else "optional")
    require_final = bool(config.get("contracts.require_final_loading", True))
    final_bound = (
        result.final_cycle is not None
        and bool(result.final_cycle_source)
        and result.final_loading_source == result.final_cycle_source
        and result.final_cycle_line_no is not None
        and result.final_loading_line_no is not None
        and result.final_loading_line_no > result.final_cycle_line_no
        and result.loading_primary_mol_kg is not None
    )
    add("final_loading_present", final_bound or not require_final, result.loading_primary_mol_kg, "absolute mol/kg mean after the same file's final-state marker" if require_final else "optional")
    add("final_source_unambiguous", not result.completion_issues, result.completion_issues, [])
    require_finite = bool(config.get("contracts.require_finite_loading", True))
    finite = result.loading_primary_mol_kg is not None and math.isfinite(result.loading_primary_mol_kg)
    add("final_loading_finite", finite or not require_finite, result.loading_primary_mol_kg, "finite" if require_finite else "optional")
    require_cycle = bool(config.get("contracts.require_expected_cycle", True))
    add("expected_final_cycle", (result.final_cycle is not None and result.final_cycle == expected_final_cycle) or not require_cycle, result.final_cycle, expected_final_cycle if require_cycle else "optional")
    uncertainty = result.uncertainty_primary_mol_kg
    add("reported_uncertainty_valid", uncertainty is None or (math.isfinite(uncertainty) and uncertainty >= 0), uncertainty, "finite and nonnegative when reported")
    reported = _reported_map(result)
    tolerance = float(config.get("contracts.cutoff_tolerance_A", 1e-6))
    requested_cutoffs = config.get(f"gas_profiles.{task.gas}.requested_cutoffs_A", config.get("contracts.requested_cutoffs_A", {})) or {}
    for field, expected in requested_cutoffs.items():
        values = reported.get(field, [])
        ok = any(isinstance(v, (int, float)) and abs(float(v) - float(expected)) <= tolerance for v in values)
        add(f"runtime_{field}", ok, values, expected)
    charge_tol = float(config.get("contracts.charge_tolerance", 1e-8))
    requested_charges = config.get(f"gas_profiles.{task.gas}.requested_charges_e", config.get("contracts.requested_charges_e", {})) or {}
    for field, expected in requested_charges.items():
        values = reported.get(field, [])
        ok = any(isinstance(v, (int, float)) and abs(float(v) - float(expected)) <= charge_tol for v in values)
        add(f"runtime_{field}", ok, values, expected)
    if bool(config.get(f"gas_profiles.{task.gas}.cbmc_constructed_positive", config.get("contracts.cbmc_constructed_positive", False))):
        constructed = []
        for row in result.move_statistics:
            if result.final_cycle_source and row.get("source") != result.final_cycle_source:
                continue
            if "swap" in str(row.get("move_type", "")) and "constructed" in row:
                constructed.append(float(row["constructed"]))
        add("cbmc_constructed_positive", any(v > 0 for v in constructed), constructed, ">0")
    failures = [c for c in checks if not c["passed"] and c["severity"] == "ERROR"]
    return {
        "status": "PASS" if not failures else "FAIL",
        "task_id": task.task_id,
        "checks": checks,
        "failure_count": len(failures),
    }
