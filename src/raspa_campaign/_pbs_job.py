"""P02 standalone PBS job footer/runner. Stdlib only; copied into each render.

This wraps the manager; it does not repair manager retry, native process-group
cleanup, or scientific input semantics. SIGKILL/power loss cannot write a footer.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1048576), b''):
            h.update(b)
    return h.hexdigest()


def regular(root, relative):
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ValueError('Invalid relative evidence path: ' + relative)
    current = Path(root)
    if current.is_symlink():
        raise ValueError('Symlink evidence root refused')
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlink evidence path refused: ' + str(current))
    if not current.is_file() or not current.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('Missing/outside evidence file: ' + str(current))
    return current


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, sort_keys=True, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def select_chunk(count, env):
    a, b = env.get('PBS_ARRAY_INDEX'), env.get('PBS_ARRAYID')
    if a is not None and b is not None and a != b:
        raise ValueError('Conflicting PBS_ARRAY_INDEX and PBS_ARRAYID')
    value = a if a is not None else b
    if count == 1:
        if value is not None:
            raise ValueError('Single non-array job must not inherit an array index')
        return 0, None
    if value is None or not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError('Missing/invalid one-based PBS array index')
    number = int(value)
    if not 1 <= number <= count:
        raise ValueError('PBS array index outside rendered chunk range')
    return number-1, number


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--spec', required=True, type=Path)
    ap.add_argument('--spec-sha256', required=True)
    args = ap.parse_args()
    # Before a trusted spec is available there is no safe output destination.
    if args.spec.is_symlink() or digest(args.spec) != args.spec_sha256:
        raise RuntimeError('Rendered job spec hash mismatch; manager not invoked')
    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    render = args.spec.parent.resolve()
    if digest(Path(__file__)) != spec['runner_sha256']:
        raise RuntimeError('Rendered job runner hash mismatch; manager not invoked')
    root = Path(spec['campaign_root'])
    log_base = root / 'logs' / 'pbs'
    for part in (root, root/'logs', log_base):
        if part.is_symlink():
            raise RuntimeError('Symlink log directory refused')
    log_base.mkdir(parents=True, exist_ok=True)
    # Never use PBS_JOBID as a filesystem path. Reruns get independent evidence.
    log_dir = Path(tempfile.mkdtemp(prefix=spec['render_id']+'_', dir=log_base))
    footer = {'schema': 'rcm-pbs-final-status-v1', 'manager_version': spec['manager_version'],
              'render_id': spec['render_id'], 'created_utc': now(),
              'pbs_job_id': os.environ.get('PBS_JOBID'),
              'pbs_array_index': os.environ.get('PBS_ARRAY_INDEX'),
              'pbs_arrayid': os.environ.get('PBS_ARRAYID'),
              'hostname': socket.gethostname(), 'python_executable': sys.executable,
              'manager_executable': spec['manager_executable'],
              'manager_run_exit_code': None, 'status_command_exit_code': None,
              'internal_chunk_index': None, 'status': 'FAILED', 'wrapper_exit_code': 3,
              'signals': [], 'log_directory': str(log_dir), 'errors': []}
    child = None
    requested_signal = None
    drain_requested = False
    active_label = None
    kill_deadline = None

    def on_signal(sig, frame):
        nonlocal requested_signal, kill_deadline, drain_requested
        footer['signals'].append({'signal': signal.Signals(sig).name, 'utc': now()})
        if sig == signal.SIGUSR1:
            drain_requested = True
            if child is not None and child.poll() is None and active_label == 'manager_run':
                try: os.kill(child.pid, sig)
                except ProcessLookupError: pass
            return
        if requested_signal is None:
            requested_signal = sig
            kill_deadline = time.monotonic() + float(spec['signal_grace_seconds'])
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
        signal.signal(sig, on_signal)

    def invoke(command, label, env, timeout=None):
        nonlocal child, active_label
        active_label = label
        with (log_dir/(label+'.stdout.log')).open('wb') as out, (log_dir/(label+'.stderr.log')).open('wb') as err:
            child = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, start_new_session=True)
            began = time.monotonic()
            while child.poll() is None:
                if requested_signal and kill_deadline is not None and time.monotonic() >= kill_deadline:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                if timeout is not None and time.monotonic()-began > timeout:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    child.wait(); child = None
                    raise RuntimeError(label + ' command timed out')
                time.sleep(0.02)
            rc = child.returncode; child = None
            return rc

    rc = 3
    try:
        job_id = os.environ.get('PBS_JOBID', '')
        if not re.fullmatch(r'[0-9]+(?:\[[0-9]*\])?(?:\.[A-Za-z0-9_.-]+)?', job_id):
            raise ValueError('Valid PBS_JOBID required; do not execute a PBS job as an ordinary local script')
        short_host = socket.gethostname().split('.')[0]
        if short_host not in spec['allowed_hosts']:
            raise ValueError('Host is outside pbs.allowed_hosts: ' + short_host)
        index, external = select_chunk(spec['chunk_count'], os.environ)
        footer['internal_chunk_index'] = index; footer['external_array_index'] = external
        for rel, h in spec['input_hashes'].items():
            if digest(regular(root, rel)) != h:
                raise ValueError('Campaign/plan/chunk changed after render: '+rel)
        # Statically pinned hashes do not replace the later task-content repair.
        manager = Path(spec['manager_executable'])
        if not manager.is_file() or not os.access(manager, os.X_OK):
            raise ValueError('Configured absolute manager executable is unavailable')
        env = dict(os.environ)
        for name in ('OMP_PROC_BIND', 'OMP_PLACES', 'GOMP_CPU_AFFINITY', 'KMP_AFFINITY', 'LD_PRELOAD', 'PYTHONPATH'):
            env.pop(name, None)
        for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            env[name] = '1'
        footer['thread_environment'] = {name: env[name] for name in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS')}
        env['RCM_ALLOCATION_NCPUS'] = str(spec['ncpus'])
        cmd = [str(manager), 'run', '--root', str(root), '--chunk', str(index),
               '--workers', str(spec['ncpus']), '--allocation', spec['walltime'], '--confirm-production']
        footer['command'] = cmd
        version_rc = invoke([str(manager), '--version'], 'manager_version', env, timeout=spec['status_timeout_seconds'])
        version_output = (log_dir/'manager_version.stdout.log').read_text().strip()
        footer['manager_version_probe'] = {'returncode': version_rc, 'stdout': version_output,
                                          'launcher_sha256': digest(manager)}
        if version_rc != 0 or version_output != 'raspa-campaign '+spec['manager_version']:
            raise ValueError('Configured manager version does not match rendered candidate')
        if requested_signal or drain_requested:
            raise RuntimeError('Interrupted/drained before manager launch')
        native_rc = invoke(cmd, 'manager_run', env)
        footer['manager_run_exit_code'] = native_rc
        rc = native_rc if native_rc >= 0 else 128-native_rc
        if requested_signal:
            rc = 128+requested_signal
        # Always attempt read-only status after a normal/nonzero manager return.
        try:
            status_rc = invoke([str(manager), 'status', '--root', str(root), '--show-tasks', '--json'],
                               'manager_status', env, timeout=spec['status_timeout_seconds'])
            footer['status_command_exit_code'] = status_rc
            if status_rc != 0:
                raise RuntimeError('Manager status command failed')
            status_data = json.loads((log_dir/'manager_status.stdout.log').read_text())
            expected = spec['chunk_task_ids'][str(index)]
            rows = {r['task_id']: r for r in status_data['tasks']}
            observed = {tid: rows.get(tid, {}).get('status_state', 'MISSING') for tid in expected}
            footer['selected_task_states'] = observed
            if not expected or any(v != 'COMPLETE' for v in observed.values()):
                raise RuntimeError('Selected chunk contains unresolved/missing task states')
        except Exception as exc:
            footer['errors'].append(str(exc))
            if rc == 0: rc = 3
        # A partial drained chunk retains a nonzero manager exit; it is not complete.
        if footer['errors'] and rc == 0: rc = 3
        footer['status'] = 'COMPLETE' if rc == 0 else 'FAILED'
    except Exception as exc:
        footer['errors'].append(type(exc).__name__+': '+str(exc))
        rc = 128+requested_signal if requested_signal else 3
    finally:
        footer['wrapper_exit_code'] = rc
        footer['ended_utc'] = now()
        footer['files'] = {p.name: digest(p) for p in sorted(log_dir.iterdir()) if p.is_file()}
        atomic_json(log_dir/'final_status.json', footer)
        print('PBS_FINAL_STATUS='+str(log_dir/'final_status.json'), flush=True)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
