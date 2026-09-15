from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .config import CampaignConfig
from .models import Task

NUMERIC_FEATURES = [
    "pressure_bar",
    "replicated_framework_atom_count",
    "prior_wall_seconds",
    "prior_uptake_mol_kg",
    "pld_A",
    "lcd_A",
    "void_fraction",
    "accessible_volume_cm3_g",
    "pore_volume_cm3_g",
    "asa_m2_g",
    "density_g_cm3",
]

GAS_BASE_SECONDS = {
    "H2": 480.0,
    "CO2": 900.0,
    "N2": 660.0,
    "O2": 660.0,
    "He": 300.0,
    "Ar": 600.0,
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def heuristic_estimate(task: Task, config: CampaignConfig) -> tuple[float, str]:
    d = task.descriptors
    prior = _num(d.get("prior_wall_seconds"), 0.0)
    if prior > 0:
        pressure_factor = max(0.45, (task.pressure_bar / max(_num(d.get("prior_pressure_bar"), 5.0), 0.01)) ** 0.30)
        return max(30.0, prior * pressure_factor), "prior_wall_seconds_scaled_by_pressure"
    base = float(config.get(f"runtime.gas_base_seconds.{task.gas}", GAS_BASE_SECONDS.get(task.gas, 600.0)))
    p = max(task.pressure_bar, 0.01)
    pressure_factor = 0.55 + 0.45 * math.log10(1.0 + 10.0 * p)
    atoms = max(_num(d.get("replicated_framework_atom_count"), d.get("framework_atom_count", 400)), 50.0)
    atom_factor = max(0.35, min(8.0, atoms / 400.0))
    uptake = max(_num(d.get("prior_uptake_mol_kg"), 0.0), 0.0)
    uptake_factor = 1.0 + min(4.0, uptake / 12.0)
    lcd = max(_num(d.get("lcd_A"), d.get("LCD", 0.0)), 0.0)
    pore_factor = 1.0 + min(1.0, lcd / 40.0)
    estimate = base * pressure_factor * (atom_factor ** 0.70) * (uptake_factor ** 0.45) * (pore_factor ** 0.15)
    return max(30.0, estimate), "heuristic_gas_pressure_atoms_uptake_pore"


@dataclass(slots=True)
class RuntimeModel:
    intercept: float
    feature_names: list[str]
    coefficients: list[float]
    means: list[float]
    scales: list[float]
    gas_offsets: dict[str, float]
    sample_count: int

    def predict(self, task: Task) -> float:
        vals = feature_vector(task, self.feature_names)
        y = self.intercept + self.gas_offsets.get(task.gas, 0.0)
        for v, mean, scale, coef in zip(vals, self.means, self.scales, self.coefficients):
            y += coef * ((v - mean) / scale)
        return max(1.0, math.expm1(y))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeModel":
        return cls(**data)


def feature_vector(task: Task, names: list[str]) -> list[float]:
    values = []
    for name in names:
        if name == "pressure_bar":
            values.append(math.log1p(task.pressure_bar))
        else:
            values.append(math.log1p(max(_num(task.descriptors.get(name), 0.0), 0.0)))
    return values


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b_i] for row, b_i in zip(a, b)]
    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(m[r][i]))
        if abs(m[pivot][i]) < 1e-12:
            continue
        m[i], m[pivot] = m[pivot], m[i]
        div = m[i][i]
        m[i] = [x / div for x in m[i]]
        for r in range(n):
            if r == i:
                continue
            factor = m[r][i]
            m[r] = [x - factor * y for x, y in zip(m[r], m[i])]
    return [m[i][-1] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_runtime_model(tasks: list[Task], wall_seconds: dict[str, float], *, ridge: float = 1.0) -> RuntimeModel | None:
    rows = [(t, wall_seconds[t.task_id]) for t in tasks if t.task_id in wall_seconds and wall_seconds[t.task_id] > 0]
    if len(rows) < 8:
        return None
    names = [n for n in NUMERIC_FEATURES if n == "pressure_bar" or any(_num(t.descriptors.get(n), 0) != 0 for t, _ in rows)]
    x = [feature_vector(t, names) for t, _ in rows]
    means = [statistics.fmean(row[j] for row in x) for j in range(len(names))]
    scales = []
    for j in range(len(names)):
        vals = [row[j] for row in x]
        s = statistics.pstdev(vals)
        scales.append(s if s > 1e-9 else 1.0)
    xs = [[(v - means[j]) / scales[j] for j, v in enumerate(row)] for row in x]
    ys = [math.log1p(sec) for _, sec in rows]
    gases = sorted({t.gas for t, _ in rows})
    global_mean = statistics.fmean(ys)
    gas_offsets = {}
    for gas in gases:
        vals = [y for (t, _), y in zip(rows, ys) if t.gas == gas]
        gas_offsets[gas] = statistics.fmean(vals) - global_mean
    centered_y = [y - gas_offsets.get(t.gas, 0.0) for (t, _), y in zip(rows, ys)]
    p = len(names) + 1
    ata = [[0.0] * p for _ in range(p)]
    atb = [0.0] * p
    for row, y in zip(xs, centered_y):
        design = [1.0] + row
        for i in range(p):
            atb[i] += design[i] * y
            for j in range(p):
                ata[i][j] += design[i] * design[j]
    for i in range(1, p):
        ata[i][i] += ridge
    beta = _solve_linear(ata, atb)
    return RuntimeModel(beta[0], names, beta[1:], means, scales, gas_offsets, len(rows))


def load_runtime_model(path: Path) -> RuntimeModel | None:
    if not path.is_file():
        return None
    return RuntimeModel.from_dict(json.loads(path.read_text()))


def historical_wall_seconds(config: CampaignConfig) -> dict[str, float]:
    from .exporter import latest_export_dir
    latest = latest_export_dir(config) / "runs.csv"
    result: dict[str, float] = {}
    if not latest.is_file():
        return result
    with latest.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                if row.get("task_id") and float(row.get("external_wall_seconds") or 0) > 0:
                    result[row["task_id"]] = float(row["external_wall_seconds"])
            except ValueError:
                pass
    return result


def estimate_tasks(tasks: list[Task], config: CampaignConfig, *, model: RuntimeModel | None = None) -> list[Task]:
    gas_priority = list(config.get("scheduling.gas_priority", []))
    rank = {gas: i for i, gas in enumerate(gas_priority)}
    for task in tasks:
        if model is not None:
            task.estimated_seconds = model.predict(task)
            reason = f"fitted_runtime_model_n={model.sample_count}"
        else:
            task.estimated_seconds, reason = heuristic_estimate(task, config)
        gas_rank = rank.get(task.gas, len(rank))
        task.priority_score = 1.0e12 - gas_rank * 1.0e9 + task.estimated_seconds
        task.priority_reason = f"gas_rank={gas_rank};{reason};longest_first_within_priority"
    return tasks
