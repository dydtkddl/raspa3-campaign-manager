"""P05 immutable attempt evidence, checked reads and derived-view selection.

Hash manifests detect drift relative to a recorded snapshot, not authenticity
against an attacker who can replace every file and every anchor as the same user.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

from .atomic import atomic_write_json
from .hashing import canonical_sha256
from .models import TaskState, utc_now
from .paths import within, no_symlinks, tree_files, read_regular, read_json_file, task_directory

ACTIVE = {'CLAIMED', 'RUNNING'}
TERMINAL = {s.value for s in TaskState} - ACTIVE - {'PENDING'}
MANIFEST = 'derived/file_manifest.json'
SEAL = 'SEALED.json'
ATTEMPT_RECORD = 'derived/attempt_result.json'
REVISION_RE = re.compile(r'rev_[0-9a-f]{64}')


class EvidenceError(ValueError):
    pass


def digest_file(path: Path) -> dict[str, Any]:
    """Bounded-memory hash; reject links, devices and file changes during read."""
    path = no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceError('Evidence must be a regular file: ' + str(path))
        h = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b''):
                h.update(block)
        after = os.fstat(fd)
        current = path.lstat()
        key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if key(before) != key(after) or key(after) != key(current):
            raise EvidenceError('Evidence changed while hashing: ' + str(path))
        no_symlinks(path)
        return {'sha256': h.hexdigest(), 'size_bytes': after.st_size}
    finally:
        os.close(fd)


def snapshot(root: Path, *, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = exclude or set()
    return [{'relative_path': p.relative_to(root).as_posix(), **digest_file(p)}
            for p in tree_files(root) if p.relative_to(root).as_posix() not in excluded]


def optional_json(path: Path, default=None):
    no_symlinks(path)
    return read_json_file(path) if path.exists() else default


def issue(kind: str, path='', expected=None, observed=None, severity='ERROR'):
    return {'type': kind, 'severity': severity, 'file': str(path),
            'expected': expected, 'observed': observed}


def verify_manifest(root: Path, rows, *, exclude: set[str], fast=False):
    problems = []
    checked = unverified = 0
    expected = {}
    if not isinstance(rows, list) or not rows:
        return [issue('EMPTY_OR_INVALID_MANIFEST', root)], 0, 0
    for r in rows:
        if not isinstance(r, dict):
            problems.append(issue('INVALID_MANIFEST_ENTRY', root)); continue
        name = r.get('relative_path')
        try:
            p = within(root, name)
            if name in exclude or name in expected:
                raise EvidenceError('Duplicate/self/excluded entry')
            expected[name] = r
            if r.get('role') in {'symlink', 'unreadable'}:
                raise EvidenceError('Manifest contains unverifiable entry')
            if not re.fullmatch('[0-9a-f]{64}', str(r.get('sha256', ''))):
                raise EvidenceError('Malformed hash')
            if type(r.get('size_bytes')) is not int or r['size_bytes'] < 0:
                raise EvidenceError('Malformed size')
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise EvidenceError('Not a regular file')
            if st.st_size != r['size_bytes']:
                problems.append(issue('SIZE_MISMATCH', name, r['size_bytes'], st.st_size))
            if fast:
                unverified += 1
            else:
                h = digest_file(p)['sha256']; checked += 1
                if h != r['sha256']:
                    problems.append(issue('HASH_MISMATCH', name, r['sha256'], h))
        except FileNotFoundError:
            problems.append(issue('MISSING_FILE', name))
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            problems.append(issue('UNSAFE_OR_INVALID_MANIFEST_ENTRY', name, observed=str(exc)))
    try:
        actual = {p.relative_to(root).as_posix() for p in tree_files(root)} - exclude
        for n in sorted(actual - expected.keys()):
            problems.append(issue('UNEXPECTED_FILE', n))
    except (ValueError, OSError) as exc:
        problems.append(issue('UNSAFE_EVIDENCE_TREE', root, observed=str(exc)))
    return problems, checked, unverified


def attempt_number(path: Path) -> int:
    m = re.fullmatch(r'attempt_([0-9]{4,8})', path.name)
    if not m or int(m[1]) < 1 or path.name != f'attempt_{int(m[1]):04d}':
        raise EvidenceError('Invalid attempt directory: ' + path.name)
    return int(m[1])


def list_attempts(config, task) -> list[Path]:
    root = within(task_directory(config.root, task.task_id), 'attempts')
    if not root.exists():
        return []
    rows = []
    for p in root.iterdir():
        within(root, p.name)
        if not p.is_dir():
            raise EvidenceError('Unexpected file in attempts: ' + p.name)
        attempt_number(p); rows.append(p)
    return sorted(rows, key=attempt_number)


def inspect_attempt(attempt: Path, *, fast=False):
    problems = []
    checked = unverified = 0
    try:
        manifest = within(attempt, MANIFEST)
        seal = optional_json(within(attempt, SEAL))
        rows = optional_json(manifest)
        if rows is None:
            problems.append(issue('UNSEALED_NO_MANIFEST', manifest))
        else:
            problems, checked, unverified = verify_manifest(
                attempt, rows, exclude={MANIFEST, SEAL}, fast=fast)
        if seal is not None:
            if (not isinstance(seal, dict) or seal.get('schema') != 'rcm-attempt-seal-p05-v1'
                    or seal.get('attempt_no') != attempt_number(attempt)):
                problems.append(issue('INVALID_SEAL', attempt))
            elif digest_file(manifest)['sha256'] != seal.get('manifest_sha256'):
                problems.append(issue('MANIFEST_ANCHOR_MISMATCH', manifest))
            record = optional_json(within(attempt, ATTEMPT_RECORD))
            if not isinstance(record, dict):
                problems.append(issue('MISSING_ATTEMPT_RECORD', attempt))
            elif seal.get('task_id') != record.get('task_id') or record.get('attempt_no') != attempt_number(attempt):
                problems.append(issue('ATTEMPT_ID_MISMATCH', attempt))
        elif rows:
            problems.append(issue('LEGACY_MANIFEST_NO_OUTER_ANCHOR', manifest, severity='WARNING'))
        if fast:
            problems.append(issue('HASHES_NOT_RECOMPUTED', attempt, severity='WARNING'))
        info = {'manifest_sha256': digest_file(manifest)['sha256'] if manifest.is_file() else None,
                'seal_sha256': digest_file(within(attempt, SEAL))['sha256'] if seal else None}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        problems.append(issue('EVIDENCE_READ_ERROR', attempt, observed=str(exc))); info = {}
    errors = sum(x['severity'] == 'ERROR' for x in problems)
    return {'status': 'FAIL' if errors else 'WARN' if problems else 'PASS',
            'error_count': errors, 'issues': problems, 'hashed_files': checked,
            'unverified_files': unverified, **info}


def require_integrity(attempt):
    report = inspect_attempt(attempt)
    if report['error_count']:
        raise EvidenceError('Attempt evidence failed integrity check: ' + json.dumps(report['issues']))
    return report


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def classify(exit_code, parsed, contract):
    if exit_code is None:
        return 'UNKNOWN'
    if type(exit_code) is not int or exit_code != 0:
        return 'FAILED'
    if not parsed or parsed.get('parse_status') == 'NO_RESULT_FILE':
        return 'ENGINE_SUCCEEDED_PARSE_INCOMPLETE'
    if contract.get('status') != 'PASS':
        return 'CONTRACT_FAILED'
    if not _finite(parsed.get('loading_primary_mol_kg')):
        return 'CONTRACT_FAILED'
    return 'COMPLETE'


def attempt_observation(attempt, status=None):
    """Never attach the newest task state or exit code to an older attempt."""
    rec = optional_json(within(attempt, ATTEMPT_RECORD))
    res = optional_json(within(attempt, 'derived/resource_usage.json'), {})
    parsed = optional_json(within(attempt, 'derived/parsed_result.json'), {})
    contract = optional_json(within(attempt, 'derived/contract_report.json'), {})
    if rec is not None:
        if (not isinstance(rec, dict) or rec.get('schema') != 'rcm-attempt-result-p05-v1'
                or rec.get('attempt_no') != attempt_number(attempt)
                or rec.get('state') not in TERMINAL
                or (rec.get('exit_code') is not None and type(rec['exit_code']) is not int)):
            raise EvidenceError('Invalid attempt-local result record')
        if rec['state'] == 'COMPLETE' and classify(rec.get('exit_code'), parsed, contract) != 'COMPLETE':
            raise EvidenceError('COMPLETE attempt contradicts original native/parser/contract evidence')
        return rec
    code = res.get('exit_code')
    state = classify(code, parsed, contract)
    source = 'legacy attempt-local resource/contract files'
    if (status and status.get('attempt_no') == attempt_number(attempt)
            and status.get('state') in {'TIMEOUT', 'CANCELLED'}):
        state = status['state']; source += ' + matching latest task interruption status'
    return {'schema': 'legacy-observation-readonly', 'attempt_no': attempt_number(attempt),
            'state': state, 'exit_code': code, 'state_source': source,
            'external_wall_seconds': res.get('external_wall_seconds')}


def policy_for(config, task, attempt):
    launch = optional_json(within(attempt, 'derived/launch_contract.json'), {})
    frozen = launch.get('evidence_policy')
    if frozen is not None:
        return frozen
    sim = optional_json(within(attempt, 'work/simulation.json'), {})
    cycles = sim.get('NumberOfCycles')
    if type(cycles) is not int or cycles < 1:
        raise EvidenceError('Staged simulation.json has no positive NumberOfCycles')
    return make_policy(config, task, cycles=cycles, provenance='legacy staged cycle + explicitly current contract policy')


def make_policy(config, task, cycles=None, provenance='launch snapshot'):
    import copy
    n = cycles if cycles is not None else int(config.get(f'gas_profiles.{task.gas}.cycles.production', config.get('cycles.production', 0)))
    return {'schema': 'rcm-evidence-policy-p05-v1', 'expected_final_cycle': n,
            'output_globs': list(config.get('execution.output_globs', ['output/output_*.txt', 'output_*.txt'])),
            'json_globs': list(config.get('execution.json_output_globs', [])),
            'contract_data': copy.deepcopy({'contracts': config.get('contracts', {}),
                              'gas_profiles': {task.gas: config.get(f'gas_profiles.{task.gas}', {})}}),
            'provenance': provenance}


def finish_attempt(config, task, attempt, *, state, exit_code, wall_seconds=None):
    """Called only for newly produced attempts; never repairs old manifests."""
    if state not in TERMINAL:
        raise EvidenceError('Only terminal attempts may be sealed')
    for n in (MANIFEST, SEAL, ATTEMPT_RECORD):
        if within(attempt, n).exists():
            raise EvidenceError('Refusing to replace existing attempt evidence: ' + n)
    rec = {'schema': 'rcm-attempt-result-p05-v1', 'task_id': task.task_id,
           'attempt_no': attempt_number(attempt), 'state': state, 'exit_code': exit_code,
           'external_wall_seconds': wall_seconds, 'ended_utc': utc_now()}
    atomic_write_json(within(attempt, ATTEMPT_RECORD), rec)
    rows = snapshot(attempt, exclude={MANIFEST, SEAL})
    atomic_write_json(within(attempt, MANIFEST), rows)
    atomic_write_json(within(attempt, SEAL), {'schema': 'rcm-attempt-seal-p05-v1',
        'task_id': task.task_id, 'attempt_no': attempt_number(attempt),
        'manifest_sha256': digest_file(within(attempt, MANIFEST))['sha256'], 'sealed_utc': utc_now()})
    require_integrity(attempt)
    return rec


@contextmanager
def task_locks(config, tasks):
    """Linux advisory locks on existing task files: previews create no lock file."""
    descriptors = []
    try:
        for t in sorted(tasks, key=lambda x: x.task_id):
            p = within(task_directory(config.root, t.task_id), 'task.json')
            # NFS exclusive locks require write-capable descriptors; never write/truncate this file.
            fd = os.open(p, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
            descriptors.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        for fd in reversed(descriptors):
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def analysis_root(config, task, attempt):
    return within(task_directory(config.root, task.task_id), 'analyses/' + attempt.name)


def current_pointer(config, task, attempt):
    return optional_json(within(analysis_root(config, task, attempt), 'current.json'))


def validate_revision(folder: Path, expected_hash=None):
    meta = read_json_file(within(folder, 'revision.json'))
    mh = digest_file(folder / 'revision.json')['sha256']
    if expected_hash is not None and mh != expected_hash:
        raise EvidenceError('Revision metadata hash mismatch')
    if (not isinstance(meta, dict) or meta.get('schema') != 'rcm-derived-revision-p05-v1'
            or REVISION_RE.fullmatch(folder.name) is None
            or folder.name != 'rev_' + canonical_sha256(meta.get('identity'))):
        raise EvidenceError('Invalid derived revision identity')
    errors, _, _ = verify_manifest(folder, meta.get('manifest'), exclude={'revision.json'})
    if errors:
        raise EvidenceError('Derived revision manifest mismatch: ' + json.dumps(errors))
    return meta


def selected_view(config, task, attempt, *, validate=True, integrity_report=None):
    integrity = integrity_report if integrity_report is not None else require_integrity(attempt)
    if integrity.get('error_count'):
        raise EvidenceError('Attempt integrity failed before derived-view selection')
    pointer = current_pointer(config, task, attempt)
    if pointer is not None:
        if not isinstance(pointer, dict):
            raise EvidenceError('Invalid revision pointer document')
        rid = pointer.get('revision_id')
        if (not isinstance(pointer, dict) or not isinstance(rid, str)
                or REVISION_RE.fullmatch(rid) is None
                or not re.fullmatch('[0-9a-f]{64}', str(pointer.get('metadata_sha256', '')))):
            raise EvidenceError('Invalid revision pointer')
        folder = within(analysis_root(config, task, attempt), rid)
        meta = validate_revision(folder, pointer.get('metadata_sha256'))
        ident = meta['identity']
        if (ident.get('task_id') != task.task_id or ident.get('attempt_no') != attempt_number(attempt)
                or ident.get('attempt_snapshot_sha256') != canonical_sha256(snapshot(attempt))):
            raise EvidenceError('Derived revision does not match current original evidence')
        parsed = read_json_file(folder / 'parsed_result.json')
        contract = read_json_file(folder / 'contract_report.json')
        ref = {'revision_id': rid, 'revision_metadata_sha256': digest_file(folder/'revision.json')['sha256']}
    else:
        folder = within(attempt, 'derived')
        parsed = optional_json(within(folder, 'parsed_result.json'), {})
        contract = optional_json(within(folder, 'contract_report.json'), {})
        ref = {'revision_id': 'original', 'revision_metadata_sha256': None}
    return {'parsed': parsed, 'contract': contract, 'folder': folder,
            'integrity': integrity, **ref}


def new_id(prefix):
    from datetime import datetime, timezone
    return prefix + '_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid.uuid4().hex
