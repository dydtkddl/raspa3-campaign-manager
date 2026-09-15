from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import CampaignConfig
from .tasks import iter_tasks, load_status, load_task
from .paths import task_directory, within, read_json_file


def campaign_status(config: CampaignConfig, *, states: set[str] | None = None, gas: str | None = None, mof_contains: str | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for task in iter_tasks(config.root):
        st = load_status(config.root, task.task_id)
        row = {**task.to_dict(), **{f"status_{k}": v for k, v in st.items()}}
        state = str(st.get("state", "UNKNOWN"))
        if states and state not in states:
            continue
        if gas and task.gas != gas:
            continue
        if mof_contains and mof_contains.lower() not in task.mof_id.lower():
            continue
        rows.append(row)
    counts = Counter(r.get("status_state", "UNKNOWN") for r in rows)
    by_gas: dict[str, Counter] = defaultdict(Counter)
    remaining_seconds = 0.0
    for row in rows:
        by_gas[str(row["gas"])][str(row.get("status_state", "UNKNOWN"))] += 1
        if row.get("status_state") != "COMPLETE":
            remaining_seconds += float(row.get("estimated_seconds") or 0)
    return {
        "campaign": config.name,
        "total": len(rows),
        "counts": dict(counts),
        "by_gas": {k: dict(v) for k, v in sorted(by_gas.items())},
        "estimated_remaining_serial_seconds": remaining_seconds,
        "tasks": rows,
    }


def latest_attempt_dir(config: CampaignConfig, task_id: str) -> Path | None:
    d = within(task_directory(config.root, task_id), "attempts")
    attempts = sorted(d.glob("attempt_*")) if d.is_dir() else []
    for p in attempts:
        within(d, p.name)
    return attempts[-1] if attempts else None


def task_detail(config: CampaignConfig, task_id: str) -> dict[str, Any]:
    d = task_directory(config.root, task_id)
    task = load_task(config.root, task_id)
    detail = {"task": task.to_dict(), "status": load_status(config.root, task_id)}
    attempts = []
    folder = within(d, "attempts")
    for entry in sorted(folder.glob("attempt_*")):
        attempt = within(folder, entry.name)
        item = {"attempt": entry.name, "path": str(attempt)}
        for name in ("launch_contract.json", "parsed_result.json", "contract_report.json", "resource_usage.json", "orchestrator_error.json"):
            p = within(attempt, "derived/" + name)
            if p.is_file():
                item[name.removesuffix(".json")] = read_json_file(p)
        attempts.append(item)
    detail["attempts"] = attempts
    return detail
