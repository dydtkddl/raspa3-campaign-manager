from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .config import CampaignConfig
from .tasks import iter_tasks


def estimate_storage(config: CampaignConfig, *, sample_paths: list[Path] | None = None, safety_factor: float = 1.25) -> dict[str, Any]:
    sizes = []
    for p in sample_paths or []:
        if p.is_file(): sizes.append(p.stat().st_size)
    if not sizes:
        for p in config.root.glob("tasks/task_*/attempts/attempt_*/work/output/output_*.txt"):
            sizes.append(p.stat().st_size)
    task_count = sum(1 for _ in iter_tasks(config.root))
    if sizes:
        ordered = sorted(sizes)
        p50 = statistics.median(ordered)
        p95 = ordered[min(len(ordered)-1, max(0, int(0.95 * len(ordered)) - 1))]
    else:
        p50 = 0; p95 = 0
    projected = int(p95 * task_count * safety_factor)
    production = int(config.get("cycles.production", 0))
    print_every = int(config.get("cycles.print_every", 1))
    reports_per_run = production // print_every if print_every else 0
    recommendation = print_every
    if p95 > 20 * 1024 * 1024 and reports_per_run > 20:
        recommendation = min(production, print_every * 2)
    report = {
        "task_count": task_count,
        "pilot_file_count": len(sizes),
        "p50_output_bytes": p50,
        "p95_output_bytes": p95,
        "safety_factor": safety_factor,
        "projected_raw_output_bytes": projected,
        "print_every": print_every,
        "status_reports_per_run": reports_per_run,
        "recommended_print_every": recommendation,
        "note": "Projection uses P95 pilot size; optional movies, histograms, RDF, MSD, density grids, and binary crash restarts must be budgeted separately.",
    }
    atomic_write_json(config.root / "reports" / "storage_estimate.json", report)
    return report
