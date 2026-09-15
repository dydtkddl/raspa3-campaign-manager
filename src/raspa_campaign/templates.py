from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
import uuid

from .atomic import atomic_write_bytes
from .config import CampaignConfig, ConfigError
from .hashing import canonical_sha256, sha256_bytes
from .input_identity import cycles_for, static_context, verify_task_inputs
from .models import Task
from .paths import attempt_directory, no_symlinks, read_regular, tree_files, within

TOKEN_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def task_environment(config: CampaignConfig, task: Task, attempt_no: int, attempt_dir: Path) -> dict[str, str]:
    c = {k: getattr(task, k) for k in ('gas','temperature_K','pressure_bar','pressure_Pa','seed','replicate')}
    allowed = {'PATH','HOME','USER','LOGNAME','SHELL','TMPDIR','LANG','LC_ALL','TZ','RASPA_DIR','RASPA2_DIR_OVERRIDE','PBS_JOBID','PBS_ARRAY_INDEX','PBS_ARRAYID'}
    inherited = {k:v for k,v in os.environ.items() if k in allowed}
    env = {**inherited, **static_context(task.mof_id, c, cycles_for(config, task.gas), task.model_branch, task.descriptors),
           'CAMPAIGN_ROOT': str(config.root), 'TASK_ID': task.task_id, 'TASK_KEY': task.task_key,
           'ATTEMPT_NO': str(attempt_no), 'ATTEMPT_DIR': str(attempt_dir), 'WORK_DIR': str(attempt_dir / 'work'),
           'ENGINE': str(config.engine_binary), 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
           'OPENBLAS_NUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1'}
    for name in ('OMP_PROC_BIND', 'OMP_PLACES', 'GOMP_CPU_AFFINITY', 'KMP_AFFINITY'):
        env.pop(name, None)
    return env


def render_text(text: str, env: dict[str, str], *, strict: bool = True) -> str:
    missing = set()
    def repl(match):
        if match.group(1) in env:
            return env[match.group(1)]
        missing.add(match.group(1))
        return match.group(0)
    rendered = TOKEN_RE.sub(repl, text)
    if strict and missing:
        raise ConfigError('Undefined template variables: ' + ', '.join(sorted(missing)))
    return rendered


def verify_staged_inputs(task: Task, attempt_dir: Path, staged: dict[str, Any], *, before_launch: bool = False) -> dict[str, Any]:
    from .input_identity import validate_task_record, InputIdentityError
    validate_task_record(task, require_identity=True)
    no_symlinks(attempt_dir)
    expected = {'work/' + r['path']: r for r in task.input_contract['staged_files']}
    rows = staged.get('files', [])
    if (staged.get('input_contract_sha256') != task.contract_sha256
            or staged.get('manifest_sha256') != canonical_sha256(rows)
            or len(rows) != len(expected) or {r.get('path') for r in rows} != set(expected)):
        raise InputIdentityError('Staging manifest is not bound to the planned inputs')
    for row in rows:
        exp = expected[row['path']]
        if row.get('sha256') != exp['sha256'] or row.get('size_bytes') != exp['size_bytes']:
            raise InputIdentityError('Staging manifest input hash differs from task')
        raw = read_regular(within(attempt_dir, row['path']), single_link=True)
        if len(raw) != exp['size_bytes'] or sha256_bytes(raw) != exp['sha256']:
            raise InputIdentityError('Staged input changed: ' + row['path'])
    if before_launch:
        names = {p.relative_to(attempt_dir).as_posix() for p in tree_files(attempt_dir / 'work')}
        if names != set(expected):
            raise InputIdentityError('Unexpected files exist in staged work directory before launch')
    return {'status': 'PASS', 'input_contract_sha256': task.contract_sha256, 'files_checked': len(expected)}


def stage_task(config: CampaignConfig, task: Task, attempt_no: int, attempt_dir: Path) -> dict[str, Any]:
    expected_dir = attempt_directory(config.root, task.task_id, attempt_no)
    if no_symlinks(attempt_dir) != expected_dir:
        raise ConfigError('Attempt staging path does not match task ID/attempt number')
    contract, payload, sources = verify_task_inputs(config, task)
    work = within(attempt_dir, 'work')
    if work.exists():
        raise FileExistsError('Work directory already exists; no overwrite')
    temp = within(attempt_dir, '.staging_' + uuid.uuid4().hex)
    temp.mkdir(parents=True, exist_ok=False)
    # Invalid templates were checked in memory first. Interrupted writes remain in
    # .staging_* for inspection and are never published as a usable work directory.
    for rel, raw in payload.items():
        dst = within(temp, rel)
        atomic_write_bytes(dst, raw)
        if read_regular(dst, single_link=True) != raw:
            raise ConfigError('Staged input write verification failed')
    if work.exists():
        raise FileExistsError('Work directory appeared during staging')
    os.rename(temp, work)
    manifest = [{'role': 'cif' if rel == task.mof_id + '.cif' else 'template', 'path': 'work/' + rel,
                 'size_bytes': len(raw), 'sha256': sha256_bytes(raw), 'source': sources[rel]}
                for rel, raw in sorted(payload.items())]
    env = task_environment(config, task, attempt_no, attempt_dir)
    keys = ['MOF_ID','CIF_PATH','CIF_NAME','GAS','TEMPERATURE_K','PRESSURE_PA','SEED','REPLICATE','WORK_DIR',
            'INITIALIZATION_CYCLES','PRODUCTION_CYCLES','PRINT_EVERY','MODEL_BRANCH','OMP_NUM_THREADS']
    staged = {'schema': 'rcm-staging-p03-v1', 'input_contract_sha256': task.contract_sha256,
              'files': manifest, 'manifest_sha256': canonical_sha256(manifest), 'environment': {k: env[k] for k in keys}}
    verify_staged_inputs(task, attempt_dir, staged, before_launch=True)
    return staged
