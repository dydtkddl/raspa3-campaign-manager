from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable
import uuid

from .atomic import atomic_write_json, atomic_write_text
from .config import CampaignConfig
from .inventory import load_inventory
from .hashing import canonical_sha256
from .models import Status, Task
from .input_identity import IDENTITY_SCHEMA, InputIdentityError, condition_checked, integer, snapshot_inputs, validate_task_record
from .paths import no_symlinks, read_json_file, task_directory, validate_task_id, within

PLANNING_FIELDS = {'estimated_seconds', 'priority_score', 'priority_reason', 'chunk_id'}


def iter_conditions(config: CampaignConfig):
    seen = set()
    for block in config.get('matrix.conditions', []):
        reps = integer(block.get('replicates', 1), 'replicates', 1)
        for t in block['temperatures_K']:
            for p in block['pressures_bar']:
                for seed in block['seeds']:
                    for replicate in range(1, reps + 1):
                        c = condition_checked(dict(gas=block['gas'], temperature_K=t, pressure_bar=p, seed=seed, replicate=replicate))
                        key = (c['gas'], c['temperature_K'], c['pressure_Pa'], c['seed'], c['replicate'])
                        if key in seen:
                            raise InputIdentityError('Duplicate normalized gas/T/P/seed/replicate condition')
                        seen.add(key)
                        yield c


def task_contract(config: CampaignConfig, record, condition: dict[str, Any]) -> dict[str, Any]:
    return snapshot_inputs(config, record, condition)[0]


def make_task(config: CampaignConfig, record, condition: dict[str, Any]) -> Task:
    c = condition_checked(condition)
    contract = task_contract(config, record, c)
    digest = canonical_sha256(contract)
    key = '__'.join([record.mof_id, c['gas'], f"{c['temperature_K']:g}K", f"{c['pressure_bar']:g}bar", f"seed{c['seed']}", f"rep{c['replicate']}"])
    return Task(task_id='task_' + digest[:20], task_key=key, mof_id=record.mof_id,
                cif_path=str(no_symlinks(Path(record.cif_path))), descriptors=record.descriptors,
                contract_sha256=digest, model_branch=contract['model_branch'],
                identity_schema=IDENTITY_SCHEMA, input_contract=contract,
                build_config_sha256=config.config_sha256, **c)


def assert_same_task(planned: Task, stored: Task) -> None:
    # Routing/bytes cannot be replaced by a modified task_index or chunk file.
    ignored = PLANNING_FIELDS | {'build_config_sha256', 'descriptors'}
    left = {k: v for k, v in planned.to_dict().items() if k not in ignored}
    right = {k: v for k, v in stored.to_dict().items() if k not in ignored}
    if left != right:
        raise InputIdentityError('Task index/plan differs from immutable task.json')


def build_tasks(config: CampaignConfig, *, limit: int | None = None) -> dict[str, Any]:
    if limit is not None:
        integer(limit, 'limit', 1)
    inventory = load_inventory(config, require_cif=True)
    conditions = list(iter_conditions(config))
    from .discovery import records_for_gas
    allowed = {g: {r.mof_id for r in records_for_gas(config, inventory, g)}
               for g in sorted({c['gas'] for c in conditions})}
    tasks = []
    for record in inventory:
        for c in conditions:
            if record.mof_id not in allowed[c['gas']]:
                continue
            tasks.append(make_task(config, record, c))
            if limit is not None and len(tasks) >= limit:
                break
        if limit is not None and len(tasks) >= limit:
            break
    if not tasks:
        raise InputIdentityError('No tasks were selected')
    root = no_symlinks(config.root)
    task_root = within(root, 'tasks')
    plans = within(root, 'plans')
    reports = within(root, 'reports')
    index = within(root, 'plans/task_index.jsonl')
    report_path = within(root, 'reports/task_build_report.json')
    existing = 0
    # Complete validation before creating a task directory or updating the index.
    for task in tasks:
        d = task_directory(root, task.task_id)
        if d.exists():
            stored = load_task(root, task.task_id)
            validate_task_record(stored, require_identity=True)
            assert_same_task(task, stored)
            load_status(root, task.task_id)
            existing += 1
    for task in tasks:
        d = task_directory(root, task.task_id)
        if d.exists():
            continue
        d.mkdir(parents=True, exist_ok=False)
        for sub in ('attempts', 'derived', 'claims'):
            (d / sub).mkdir()
        atomic_write_json(d / 'task.json', task.to_dict())
        atomic_write_json(d / 'status.json', Status(task_id=task.task_id).to_dict())
    selected = {t.task_id for t in tasks}
    inactive = [p.name for p in sorted(task_root.iterdir()) if p.is_dir() and p.name.startswith('task_') and p.name not in selected]
    report = {'campaign': config.name, 'task_count': len(tasks), 'existing_task_count': existing,
              'new_task_count': len(tasks) - existing, 'config_sha256': config.config_sha256,
              'identity_schema': IDENTITY_SCHEMA, 'inactive_task_directories_preserved': inactive,
              'note': 'task_index selects this build; earlier task directories/attempts are retained, not migrated'}
    atomic_write_text(index, ''.join(json.dumps(t.to_dict(), ensure_ascii=False, sort_keys=True) + '\n' for t in tasks))
    atomic_write_json(report_path, report)
    return {'task_count': len(tasks), 'existing': existing, 'new': len(tasks) - existing,
            'inactive_preserved': len(inactive), 'identity_schema': IDENTITY_SCHEMA}


def load_task(root: Path, task_id: str) -> Task:
    d = task_directory(root, task_id)
    data = read_json_file(d / 'task.json')
    if data.get('task_id') != task_id:
        raise InputIdentityError('Task file ID differs from its directory')
    task = Task(**data)
    validate_task_record(task)
    return task


def load_status(root: Path, task_id: str) -> dict[str, Any]:
    data = read_json_file(task_directory(root, task_id) / 'status.json')
    if data.get('task_id') != task_id:
        raise InputIdentityError('Status task ID differs from its directory')
    return data


def iter_tasks(root: Path) -> Iterable[Task]:
    index = within(root, 'plans/task_index.jsonl')
    if index.is_file():
        from .paths import read_regular
        seen = set()
        for line in read_regular(index).decode('utf-8').splitlines():
            if not line.strip():
                continue
            task = Task(**json.loads(line))
            validate_task_record(task)
            if task.task_id in seen:
                raise InputIdentityError('Duplicate task ID in task index')
            seen.add(task.task_id)
            assert_same_task(task, load_task(root, task.task_id))
            yield task
        return
    d = within(root, 'tasks')
    if not d.exists():
        return
    for p in sorted(d.iterdir()):
        if p.name.startswith('task_'):
            yield load_task(root, p.name)
