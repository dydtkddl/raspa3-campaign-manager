from __future__ import annotations

import heapq
import json
import math
import os
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, atomic_write_text, atomic_write_bytes
from .hashing import canonical_sha256, sha256_bytes
from .config import CampaignConfig, ConfigError
from .models import Task
from .runtime import estimate_tasks, fit_runtime_model, historical_wall_seconds, load_runtime_model
from .tasks import iter_tasks, load_task, assert_same_task
from .paths import within, read_regular, tree_files
from .input_identity import validate_task_record, InputIdentityError


def parse_duration(text: str | int | float) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1:] in units:
        return float(s[:-1]) * units[s[-1]]
    parts = s.split(":")
    if len(parts) == 3:
        h, m, sec = map(float, parts)
        return h * 3600 + m * 60 + sec
    return float(s)


def _sort_key(task: Task, config: CampaignConfig):
    mode = str(config.get("scheduling.priority_mode", "soft")).lower()
    priorities = {g: i for i, g in enumerate(config.get("scheduling.gas_priority", []))}
    if mode == "runtime-only":
        return (-task.estimated_seconds, task.task_id)
    return (priorities.get(task.gas, len(priorities)), -task.estimated_seconds, -float(task.descriptors.get("replicated_framework_atom_count") or 0), task.task_id)


def lpt_chunks(tasks: list[Task], n_chunks: int) -> list[dict[str, Any]]:
    n_chunks = max(1, min(n_chunks, len(tasks) or 1))
    heap: list[tuple[float, int, list[Task]]] = [(0.0, i, []) for i in range(n_chunks)]
    heapq.heapify(heap)
    for task in sorted(tasks, key=lambda t: (-t.estimated_seconds, t.task_id)):
        total, idx, assigned = heapq.heappop(heap)
        assigned.append(task)
        task.chunk_id = idx
        heapq.heappush(heap, (total + task.estimated_seconds, idx, assigned))
    result = []
    for total, idx, assigned in sorted(heap, key=lambda x: x[1]):
        result.append({
            "chunk_id": idx,
            "predicted_serial_seconds": total,
            "task_count": len(assigned),
            "tasks": assigned,
        })
    return result


def _ordered_balanced_chunks(tasks: list[Task], n_chunks: int) -> list[dict[str, Any]]:
    """Place tasks in chosen queue order; balance loads without re-sorting by cost."""
    heap = [(0.0, i) for i in range(n_chunks)]
    heapq.heapify(heap)
    assigned = [[] for _ in range(n_chunks)]
    totals = [0.0] * n_chunks
    for task in tasks:
        total, index = heapq.heappop(heap)
        task.chunk_id = index
        assigned[index].append(task)
        totals[index] = total + task.estimated_seconds
        heapq.heappush(heap, (totals[index], index))
    return [{'chunk_id':i,'predicted_serial_seconds':totals[i],
             'task_count':len(assigned[i]),'tasks':assigned[i]} for i in range(n_chunks)]


def _selected_digest(tasks):
    return canonical_sha256([{'task_id':t.task_id,'cif_path':t.cif_path,'descriptors':t.descriptors}
                             for t in tasks])


def plan_campaign(config: CampaignConfig, *, n_chunks: int | None = None,
                  refit: bool = True, preview: bool = False) -> dict[str, Any]:
    from .ordering import ordered_tasks, order_policy
    from .tasks import load_status
    tasks = list(iter_tasks(config.root))
    if not tasks:
        raise RuntimeError('No tasks exist; run build first')
    selected_digest = _selected_digest(tasks)
    model_path = within(config.root, 'plans/runtime_model.json')
    model = load_runtime_model(model_path)
    if refit:
        fitted = fit_runtime_model(tasks, historical_wall_seconds(config))
        if fitted is not None:
            model = fitted
    estimate_tasks(tasks, config, model=model)
    policy = order_policy(config)
    chosen = ordered_tasks(tasks, config)
    tasks = chosen if chosen is not None else sorted(tasks, key=lambda t: _sort_key(t, config))
    if any(not math.isfinite(t.estimated_seconds) or t.estimated_seconds<0 for t in tasks):
        raise ConfigError('Runtime estimate must be finite and nonnegative')
    if n_chunks is None:
        configured = config.get('scheduling.number_of_chunks', 0) or 0
        if type(configured) is not int or configured<0:
            raise ConfigError('scheduling.number_of_chunks must be a nonnegative integer')
        if configured:
            n_chunks=configured
        else:
            workers=config.get('scheduling.workers_per_chunk',config.get('execution.workers',1))
            target=parse_duration(config.get('scheduling.target_chunk_walltime','8h'))
            if type(workers) is not int or workers<1 or not math.isfinite(target) or target<=0:
                raise ConfigError('Invalid workers/target_chunk_walltime')
            n_chunks=max(1,math.ceil(sum(t.estimated_seconds for t in tasks)/(workers*target)))
    if type(n_chunks) is not int or n_chunks<1:
        raise ConfigError('chunks must be a positive integer')
    n_chunks=min(n_chunks,len(tasks))
    rank={t.task_id:i for i,t in enumerate(tasks)}
    if policy is not None:
        for t in tasks:
            t.priority_score=float(len(tasks)-rank[t.task_id])
            t.priority_reason='explicit '+policy['order']+'; rank='+str(rank[t.task_id])
        chunks=_ordered_balanced_chunks(tasks,n_chunks)
    else:
        chunks=lpt_chunks(tasks,n_chunks)
    queue_files={}
    def lines(rows):
        return ''.join(json.dumps(t.to_dict(),ensure_ascii=False,sort_keys=True,allow_nan=False)+'\n' for t in rows).encode()
    for chunk in chunks:
        ordered=sorted(chunk['tasks'],key=lambda t:rank[t.task_id])
        queue_files[f"chunks/chunk_{chunk['chunk_id']:05d}.jsonl"]=lines(ordered)
    queue_files['plans/planned_tasks.jsonl']=lines(tasks)
    plan={'schema':4,'campaign':config.name,'config_sha256':config.config_sha256,
          'priority_mode':config.get('scheduling.priority_mode','soft'),'gas_priority':config.get('scheduling.gas_priority',[]),
          'order_policy':policy,'selected_input_digest':selected_digest,'task_count':len(tasks),'chunk_count':len(chunks),
          'predicted_serial_seconds':sum(t.estimated_seconds for t in tasks),
          'max_chunk_serial_seconds':max(c['predicted_serial_seconds'] for c in chunks),
          'min_chunk_serial_seconds':min(c['predicted_serial_seconds'] for c in chunks),
          'runtime_model':model.to_dict() if model else None,
          'chunks':[{k:v for k,v in c.items() if k!='tasks'} for c in chunks],
          'queue_files':{p:sha256_bytes(raw) for p,raw in sorted(queue_files.items())},
          'ordering_boundary':'Global list and each chunk preserve chosen queue rank; concurrent starts/finishes and PBS subjob start order are not guaranteed.'}
    plan['revision_id']='plan_'+canonical_sha256(plan)[:24]
    if preview:
        return {**plan,'preview':True,'first_tasks':[{'task_id':t.task_id,'mof_id':t.mof_id,'gas':t.gas,
                 'pressure_bar':t.pressure_bar,'chunk_id':t.chunk_id,'descriptors':t.descriptors} for t in tasks[:20]],'writes':False}
    if any(load_status(config.root,t.task_id).get('state') in {'RUNNING','CLAIMED'} for t in tasks):
        raise ConfigError('Active tasks exist; no replan during execution')
    plans=within(config.root,'plans');plans.mkdir(parents=True,exist_ok=True)
    lock=within(config.root,'plans/.p04_plan_lock')
    try:lock.mkdir()
    except FileExistsError:raise ConfigError('Another planner or interrupted plan owns .p04_plan_lock; inspect before retry') from None
    try:
        if _selected_digest(list(iter_tasks(config.root)))!=selected_digest:
            raise ConfigError('Input index changed during planning')
        revision=within(config.root,'plans/revisions/'+plan['revision_id'])
        payload=dict(queue_files)
        payload['plan.json']=(json.dumps(plan,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
        if revision.exists():
            actual={p.relative_to(revision).as_posix():sha256_bytes(read_regular(p)) for p in tree_files(revision)}
            if actual!={p:sha256_bytes(raw) for p,raw in payload.items()}:
                raise ConfigError('Existing plan revision was edited; no overwrite')
        else:
            temporary=within(config.root,'plans/revisions/.building_'+uuid.uuid4().hex)
            temporary.mkdir(parents=True,exist_ok=False)
            for p,raw in payload.items():atomic_write_bytes(within(temporary,p),raw)
            os.rename(temporary,revision)
        for p,raw in queue_files.items():atomic_write_bytes(within(config.root,p),raw)
        # Commit marker is published last. A partial publication is rejected at load.
        atomic_write_bytes(within(config.root,'plans/plan.json'),payload['plan.json'])
    finally:
        lock.rmdir()
    return plan


def load_planned_tasks(config: CampaignConfig, chunk_id: int | None = None) -> list[Task]:
    plan_path=within(config.root,'plans/plan.json')
    if not plan_path.is_file():raise FileNotFoundError('Plan missing; run plan first')
    plan=json.loads(read_regular(plan_path))
    if plan.get('config_sha256')!=config.config_sha256:
        raise InputIdentityError('Stale plan: configuration changed')
    if chunk_id is not None:
        if type(chunk_id) is not int or not 0<=chunk_id<plan.get('chunk_count',0):
            raise InputIdentityError('Invalid chunk index')
        rel=f'chunks/chunk_{chunk_id:05d}.jsonl'
    else:rel='plans/planned_tasks.jsonl'
    path=within(config.root,rel)
    raw=read_regular(path)
    current_tasks=list(iter_tasks(config.root))
    if plan.get('schema')==4:
        if plan['selected_input_digest']!=_selected_digest(current_tasks):
            raise InputIdentityError('Stale plan: selected descriptors/input order changed; replan')
        if plan.get('queue_files',{}).get(rel)!=sha256_bytes(raw):
            raise InputIdentityError('Planned queue content/order hash mismatch')
        rid=plan.get('revision_id','')
        if re.fullmatch(r'plan_[0-9a-f]{24}',rid) is None:raise InputIdentityError('Invalid plan revision ID')
        revision=within(config.root,'plans/revisions/'+rid)
        if read_regular(revision/'plan.json')!=read_regular(plan_path) or read_regular(within(revision,rel))!=raw:
            raise InputIdentityError('Plan pointer differs from preserved plan revision')
    rows=[Task(**json.loads(line)) for line in raw.decode('utf-8').splitlines() if line.strip()]
    current={t.task_id for t in current_tasks};seen=set()
    for task in rows:
        validate_task_record(task,require_identity=True)
        assert_same_task(task,load_task(config.root,task.task_id))
        if task.task_id in seen or task.task_id not in current:raise InputIdentityError('Duplicate or stale planned task ID')
        if chunk_id is not None and task.chunk_id!=chunk_id:raise InputIdentityError('Task assigned to wrong chunk')
        seen.add(task.task_id)
    if chunk_id is None and seen!=current:raise InputIdentityError('Stale plan task index')
    return rows
