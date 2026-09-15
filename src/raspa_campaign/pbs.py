from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import uuid
from typing import Any

from . import __version__
from .atomic import atomic_write_json, atomic_write_text
from .config import CampaignConfig, ConfigError
from .hashing import sha256_bytes, sha256_file
from .models import utc_now
from ._pbs_job import regular


def _scalar(key: str, value: Any, pattern: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or len(value)>200:
        raise ConfigError(f'{key}: expected a nonempty bounded string')
    if value and not re.fullmatch(pattern, value):
        raise ConfigError(f'{key}: unsupported characters or control character')
    return value


def _integer(key: str, value: Any, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise ConfigError(f'{key}: integer {lo}..{hi} required, got {value!r}')
    return value


def _linux_path(key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.startswith('/') or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ConfigError(f'{key}: explicit absolute Linux path required')
    if '\\' in value or '$' in value or '..' in PurePosixPath(value).parts or re.match(r'^/mnt/[a-zA-Z](?:/|$)', value):
        raise ConfigError(f'{key}: Windows-mounted, unexpanded or traversing path refused')
    return value


def _inputs(config: CampaignConfig):
    root = config.root
    plan_path = regular(root, 'plans/plan.json')
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    chunks = _integer('plan.chunk_count', plan.get('chunk_count'), 1, 100000)
    if plan.get('config_sha256') != config.config_sha256:
        raise ConfigError('Stale plan: config changed. Review settings and run plan again.')
    if [c.get('chunk_id') for c in plan.get('chunks', [])] != list(range(chunks)):
        raise ConfigError('Plan chunks must cover internal indexes 0..N-1 exactly')
    hashes = {'plans/plan.json': sha256_file(plan_path),
              'campaign.toml': sha256_file(regular(root, 'campaign.toml'))}
    task_ids = {}; seen = set()
    for c in plan['chunks']:
        rel = f"chunks/chunk_{c['chunk_id']:05d}.jsonl"
        file = regular(root, rel); hashes[rel] = sha256_file(file)
        records = [json.loads(line) for line in file.read_text().splitlines() if line.strip()]
        ids = []
        for row in records:
            tid = row.get('task_id', '')
            if not re.fullmatch(r'task_[a-f0-9]{20}', tid) or tid in seen or row.get('chunk_id') != c['chunk_id']:
                raise ConfigError('Malformed, duplicate or misassigned chunk task')
            seen.add(tid); ids.append(tid)
        if not ids or len(ids) != c.get('task_count'):
            raise ConfigError('Empty/inconsistent chunk task count')
        task_ids[str(c['chunk_id'])] = ids
    if len(seen) != plan.get('task_count'):
        raise ConfigError('Plan task count differs from chunk files')
    return plan, hashes, task_ids


def render_pbs(config: CampaignConfig, *, preview: bool = False) -> dict[str, Any]:
    """Render a reviewed GA profile. P02 does not claim scheduler qualification."""
    if config.get('pbs.profile') != 'ga-openpbs':
        raise ConfigError('pbs.profile="ga-openpbs" must be explicitly selected; no dialect guessing')
    manager = _linux_path('pbs.manager_executable', config.get('pbs.manager_executable'))
    python = _linux_path('pbs.python_executable', config.get('pbs.python_executable'))
    name = _scalar('pbs.job_name', config.get('pbs.job_name', 'raspa_campaign'), r'[A-Za-z][A-Za-z0-9_.-]{0,63}')
    queue = _scalar('pbs.queue', config.get('pbs.queue', ''), r'[A-Za-z0-9_][A-Za-z0-9_.-]*', empty=True)
    project = _scalar('pbs.project', config.get('pbs.project', ''), r'[A-Za-z0-9_][A-Za-z0-9_.-]*', empty=True)
    ncpus = _integer('pbs.ncpus', config.get('pbs.ncpus', 1), 1, 36)
    wall = _scalar('pbs.walltime', config.get('pbs.walltime', '08:00:00'), r'[0-9]{2,4}:[0-5][0-9]:[0-5][0-9]')
    if sum(int(v)*f for v,f in zip(wall.split(':'), (3600,60,1)))<=0:
        raise ConfigError('pbs.walltime must be positive')
    memory = _scalar('pbs.memory', config.get('pbs.memory', ''), r'[1-9][0-9]*(?:kb|mb|gb|tb)', empty=True)
    hosts = config.get('pbs.allowed_hosts', ['ga01','ga02','ga03','ga04'])
    if not isinstance(hosts, list) or not hosts or any(not isinstance(h,str) for h in hosts) or len(set(hosts)) != len(hosts):
        raise ConfigError('pbs.allowed_hosts: unique nonempty hostname list required')
    for host in hosts:
        _scalar('pbs.allowed_hosts', host, r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}')
        if host=='ga00': raise ConfigError('ga00 is a control host, not an execution target')
    host = _scalar('pbs.host', config.get('pbs.host', ''), r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}', empty=True)
    if host and host not in hosts:
        raise ConfigError('pbs.host is outside pbs.allowed_hosts')
    for key in ('module_commands','extra_directives'):
        if config.get('pbs.'+key, []) != []:
            raise ConfigError('pbs.'+key+': P02 refuses raw shell/directives; use explicit executable/resource fields')
    plan, hashes, tasks = _inputs(config)
    count = plan['chunk_count']
    cap = _integer('pbs.array_concurrency', config.get('pbs.array_concurrency', 1), 1, 100000)
    if count>1 and cap<count:
        raise ConfigError('GA %concurrency syntax is unverified. Smaller-than-array concurrency is not implemented in P02; split the plan or review a full-array resource budget. Nothing submitted.')
    sid = 'render_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'_'+uuid.uuid4().hex[:8]
    folder = config.root/'pbs'/sid
    from importlib.resources import files
    import math
    from .execution_control import positive_number
    runtime = files('raspa_campaign').joinpath('_pbs_job.py').read_bytes()
    native_grace = positive_number(config.get('execution.kill_grace_seconds',30),'execution.kill_grace_seconds')
    # The outer wrapper must not kill the manager before its native groups can be cleaned.
    outer_grace = max(_integer('pbs.signal_grace_seconds',config.get('pbs.signal_grace_seconds',5),1,3600),
                      math.ceil(native_grace)+30)
    spec = {'schema':'rcm-pbs-job-spec-v1','render_id':sid,'manager_version':__version__,
            'campaign_root':str(config.root),'manager_executable':manager,'python_executable':python,
            'chunk_count':count,'chunk_task_ids':tasks,'input_hashes':hashes,
            'runner_sha256':sha256_bytes(runtime),'allowed_hosts':hosts,'ncpus':ncpus,'walltime':wall,
            'status_timeout_seconds':_integer('pbs.status_timeout_seconds',config.get('pbs.status_timeout_seconds',30),1,300),
            'signal_grace_seconds':outer_grace,'native_kill_grace_seconds':native_grace}
    spec_bytes=(json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()
    select=f'select=1:ncpus={ncpus}'+(f':mem={memory}' if memory else '')+(f':host={host}' if host else '')
    lines=['#!/usr/bin/env bash',f'#PBS -N {name}',f'#PBS -l {select}',f'#PBS -l walltime={wall}','#PBS -j oe','#PBS -k oe']
    if queue: lines.append(f'#PBS -q {queue}')
    if project: lines.append(f'#PBS -P {project}')
    if count>1: lines.append(f'#PBS -J 1-{count}')
    lines += ['set -euo pipefail',
              '# Final status is written directly under campaign/logs/pbs by the stdlib runner.',
              'unset OMP_PROC_BIND OMP_PLACES GOMP_CPU_AFFINITY KMP_AFFINITY LD_PRELOAD PYTHONPATH',
              'export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1',
              'exec '+shlex.quote(python)+' -I -S -B '+shlex.quote(str(folder/'job_runner.py'))+
              ' --spec '+shlex.quote(str(folder/'job_spec.json'))+' --spec-sha256 '+sha256_bytes(spec_bytes)]
    script='\n'.join(lines)+'\n'
    receipt={'schema':'rcm-pbs-render-v1','created_utc':utc_now(),'render_id':sid,
             'script':str(folder/'run.pbs'),'chunk_count':count,'ncpus':ncpus,'walltime':wall,
             'array':f'1-{count}' if count>1 else None,'external_index_base':1 if count>1 else None,
             'internal_index_base':0,'array_concurrency_requested':cap,'max_subjobs_in_render':count,
             'manager_executable':manager,'python_executable':python,'input_hashes':hashes,
             'files':{'run.pbs':sha256_bytes(script.encode()),'job_spec.json':sha256_bytes(spec_bytes),
                      'job_runner.py':sha256_bytes(runtime)},'preview':preview,'script_preview':script,
             'real_pbs_qualification':'NOT_RUN'}
    if preview: return receipt
    for p in (config.root/'pbs',folder):
        if p.is_symlink(): raise ConfigError('Symlink PBS directory refused')
    folder.mkdir(parents=True,exist_ok=False)
    atomic_write_text(folder/'job_runner.py',runtime.decode())
    atomic_write_text(folder/'job_spec.json',spec_bytes.decode())
    atomic_write_text(folder/'run.pbs',script,mode=0o755)
    atomic_write_json(folder/'render_receipt.json',receipt)
    atomic_write_json(config.root/'pbs'/'pbs_render_receipt.json',receipt)
    return receipt


def submit_pbs(config: CampaignConfig, *, yes: bool = False) -> dict[str, Any]:
    if not yes:
        raise PermissionError('PBS submission requires explicit --yes; render/inspect first')
    if config.get('pbs.enabled') is not True or config.get('campaign.production_authorized') is not True:
        raise PermissionError('Submission requires pbs.enabled=true and campaign.production_authorized=true after review')
    p = regular(config.root,'pbs/pbs_render_receipt.json')
    rendered = json.loads(p.read_text())
    rid = rendered.get('render_id','')
    if not re.fullmatch(r'render_[0-9]{8}T[0-9]{12}Z_[0-9a-f]{8}',rid):
        raise ConfigError('Unsupported render receipt; render with P02 first')
    folder = config.root/'pbs'/rid
    stored=json.loads(regular(config.root,'pbs/'+rid+'/render_receipt.json').read_text())
    if stored!=rendered: raise ConfigError('Render pointer differs from immutable receipt')
    for name,h in rendered['files'].items():
        if sha256_file(regular(folder,name))!=h: raise ConfigError('Rendered file hash mismatch: '+name)
    _plan, hashes, _tasks = _inputs(config)
    if hashes!=rendered['input_hashes']: raise ConfigError('Stale render: input/plan/chunk content changed')
    raw=config.get('pbs.qsub_command','qsub')
    if raw=='qsub': qsub=shutil.which(raw)
    else: qsub=_linux_path('pbs.qsub_command',raw)
    if not qsub or not os.access(qsub,os.X_OK): raise ConfigError('qsub unavailable; run submission on GA control host later')
    lock=folder/'SUBMISSION_ATTEMPTED.json'
    receipt={'schema':'rcm-pbs-submit-v1','render_id':rid,'created_utc':utc_now(),
             'command':[qsub,str(folder/'run.pbs')],'status':'UNKNOWN_SUBMISSION'}
    # An interrupted or ambiguous qsub is never automatically retried.
    with lock.open('x',encoding='utf-8') as f: json.dump(receipt,f); f.flush(); os.fsync(f.fileno())
    try:
        cp=subprocess.run(receipt['command'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,timeout=60)
        receipt.update(returncode=cp.returncode,stdout=cp.stdout,stderr=cp.stderr)
        job=cp.stdout.strip()
        if cp.returncode!=0: receipt['status']='FAILED'
        elif re.fullmatch(r'[0-9]+(?:\[\])?(?:\.[A-Za-z0-9_.-]+)?',job):
            receipt.update(status='SUBMITTED',pbs_job_id=job)
    except Exception as exc:
        receipt['error']=type(exc).__name__+': '+str(exc)
    atomic_write_json(folder/'submit_receipt.json',receipt)
    atomic_write_json(config.root/'pbs'/'pbs_submit_receipt.json',receipt)
    if receipt['status']!='SUBMITTED':
        raise RuntimeError('PBS '+receipt['status']+'; inspect '+str(folder/'submit_receipt.json')+' before any new submission')
    return receipt
