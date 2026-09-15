from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .config import CampaignConfig


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def monotonicity_qc(config: CampaignConfig, *, tolerance_abs: float = 0.0, tolerance_sigma: float = 2.0, input_csv: Path | None = None) -> dict[str, Any]:
    from .exporter import latest_export_dir
    path = input_csv or latest_export_dir(config) / "runs.csv"
    if not path.is_file():
        raise FileNotFoundError(f"Runs CSV not found: {path}")
    rows = _read_csv(path)
    groups: dict[tuple[str, str, float, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            value = float(row.get("loading_primary_mol_kg") or "nan")
            if not math.isfinite(value): continue
            key = (row.get("mof_id", ""), row.get("gas", ""), float(row.get("temperature_K") or 0), int(float(row.get("seed") or 0)))
            groups[key].append({
                "pressure_bar": float(row.get("pressure_bar") or 0),
                "uptake": value,
                "uncertainty": float(row.get("uncertainty_primary_mol_kg") or 0),
                "engine": row.get("engine_version") or row.get("engine") or "unknown",
                "task_id": row.get("task_id"),
            })
        except (ValueError, TypeError):
            continue
    violations = []
    checks = 0
    for key, points in groups.items():
        points.sort(key=lambda r: r["pressure_bar"])
        for low, high in zip(points, points[1:]):
            checks += 1
            allowed = max(tolerance_abs, tolerance_sigma * math.sqrt(low["uncertainty"]**2 + high["uncertainty"]**2))
            delta = high["uptake"] - low["uptake"]
            if delta < -allowed:
                violations.append({
                    "mof_id": key[0], "gas": key[1], "temperature_K": key[2], "seed": key[3],
                    "lower_pressure_bar": low["pressure_bar"], "lower_uptake": low["uptake"], "lower_engine": low["engine"],
                    "higher_pressure_bar": high["pressure_bar"], "higher_uptake": high["uptake"], "higher_engine": high["engine"],
                    "delta": delta, "allowed_negative_delta": allowed,
                    "cross_engine": low["engine"] != high["engine"],
                    "recommendation": "Recalculate adjacent pressure points with the same qualified RASPA3 contract and multiple seeds before replacing legacy values." if low["engine"] != high["engine"] else "Inspect convergence, units, force-field contract, and rerun with multiple seeds.",
                })
    report = {
        "schema": 2,
        "source": str(path),
        "group_count": len(groups),
        "adjacent_pair_checks": checks,
        "violation_count": len(violations),
        "status": "PASS" if not violations else "REVIEW",
        "interpretation_boundary": "Monotonicity violations are warning signals. Monotonicity alone does not establish cross-engine parity.",
        "violations": violations,
    }
    atomic_write_json(config.root / "reports" / "monotonicity_qc.json", report)
    return report


def cross_engine_qc(config: CampaignConfig, legacy_csv: Path, new_csv: Path, *, relative_tolerance: float = 0.10, absolute_tolerance: float = 0.05) -> dict[str, Any]:
    old = _read_csv(legacy_csv); new = _read_csv(new_csv)
    def index(rows):
        out = {}
        for r in rows:
            try:
                key = (r.get("mof_id"), r.get("gas"), round(float(r.get("temperature_K") or 0), 6), round(float(r.get("pressure_bar") or 0), 8), int(float(r.get("seed") or 0)))
                val = float(r.get("loading_primary_mol_kg") or r.get("uptake_mol_kg") or "nan")
                if math.isfinite(val): out[key] = val
            except (ValueError, TypeError): pass
        return out
    a=index(old); b=index(new)
    comparisons=[]
    for key in sorted(a.keys() & b.keys(), key=str):
        av=a[key]; bv=b[key]; diff=bv-av; denom=max(abs(av), absolute_tolerance)
        rel=abs(diff)/denom
        ok=abs(diff)<=absolute_tolerance or rel<=relative_tolerance
        comparisons.append({"mof_id":key[0],"gas":key[1],"temperature_K":key[2],"pressure_bar":key[3],"seed":key[4],"legacy":av,"new":bv,"difference":diff,"relative_difference":rel,"within_tolerance":ok})
    failures=[r for r in comparisons if not r["within_tolerance"]]
    report={"schema":2,"legacy":str(legacy_csv),"new":str(new_csv),"matched":len(comparisons),"outside_tolerance":len(failures),"status":"PASS" if comparisons and not failures else "REVIEW","comparison_contract":{"relative_tolerance":relative_tolerance,"absolute_tolerance":absolute_tolerance},"note":"This is a numerical screening check, not an automatic scientific equivalence declaration.","comparisons":comparisons}
    atomic_write_json(config.root/"reports"/"cross_engine_qc.json",report)
    return report
