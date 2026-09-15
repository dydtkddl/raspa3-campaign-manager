"""P05 two-step reparsing. Original attempts and task statuses are never edited."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Any

from . import __version__
from .atomic import atomic_write_json
from .config import CampaignConfig
from .contracts import evaluate_contract
from .evidence import (ACTIVE, EvidenceError, analysis_root, attempt_number,
    attempt_observation, classify, current_pointer, digest_file, list_attempts,
    new_id, policy_for, require_integrity, selected_view, snapshot, task_locks, validate_revision)
from .hashing import canonical_sha256
from .models import utc_now
from .parser import parse_attempt
from .paths import within, no_symlinks, read_json_file, task_directory, validate_task_id
from .tasks import iter_tasks, load_status


def implementation_hashes():
    # Runtime source may be installed files or resources inside a PYZ archive.
    # The same source bytes yield the same provenance in both representations.
    from importlib.resources import files
    from .hashing import sha256_bytes
    base = files('raspa_campaign')
    return {n: sha256_bytes(base.joinpath(n).read_bytes()) for n in
            ('parser.py', 'models.py', 'contracts.py', 'reparse.py', 'evidence.py')}


def _safe_globs(patterns):
    if not isinstance(patterns, list):
        raise EvidenceError('Output patterns must be a list')
    for g in patterns:
        p = PurePosixPath(g)
        if (not g or p.is_absolute() or '..' in p.parts or '\\' in g or ':' in g
                or any(ord(c) < 32 for c in g)):
            raise EvidenceError('Unsafe output pattern')
    return patterns


def _tasks(config, ids):
    tasks = list(iter_tasks(config.root))
    if ids is not None:
        if not ids or len(ids) != len(set(ids)):
            raise EvidenceError('Task selection must be nonempty and unique')
        for t in ids:
            validate_task_id(t)
        unknown = set(ids) - {t.task_id for t in tasks}
        if unknown:
            raise EvidenceError('Unknown task selection: ' + ','.join(sorted(unknown)))
        tasks = [t for t in tasks if t.task_id in ids]
    return sorted(tasks, key=lambda t: t.task_id)


def _calculate(config, task, attempt, status):
    integrity = require_integrity(attempt)
    before = snapshot(attempt)
    if current_pointer(config, task, attempt):
        selected_view(config, task, attempt)
    policy = policy_for(config, task, attempt)
    parsed, json_docs = parse_attempt(attempt, _safe_globs(policy['output_globs']),
                    expected_final_cycle=policy['expected_final_cycle'],
                    json_globs=_safe_globs(policy['json_globs']))
    evaluation_config = CampaignConfig(config.root, config.path, copy.deepcopy(policy['contract_data']))
    observation = attempt_observation(attempt, status)
    code = observation.get('exit_code')
    contract = evaluate_contract(evaluation_config, task, parsed, exit_code=code,
                                expected_final_cycle=policy['expected_final_cycle'])
    proposed = classify(code, parsed.to_dict(), contract)
    if observation.get('state') in {'TIMEOUT', 'CANCELLED'}:
        proposed = observation['state']  # A salvage parse never erases interruption.
    identity = {'task_id': task.task_id, 'attempt_no': attempt_number(attempt),
        'attempt_snapshot_sha256': canonical_sha256(before), 'policy': policy,
        'task_contract_sha256': task.contract_sha256, 'implementation': implementation_hashes(),
        'manager_version': __version__}
    if snapshot(attempt) != before:
        raise EvidenceError('Attempt changed during reparse preview')
    original = read_json_file(attempt/'derived/contract_report.json') if (attempt/'derived/contract_report.json').is_file() else {}
    row = {'task_id': task.task_id, 'attempt_no': attempt_number(attempt),
           'revision_id': 'rev_' + canonical_sha256(identity), 'identity': identity,
           'old_contract_status': original.get('status'), 'new_contract_status': contract['status'],
           'native_state': observation['state'], 'proposed_interpretation_state': proposed,
           'task_state_will_change': False, 'last_cycle': parsed.last_cycle,
           'loading_primary_mol_kg': parsed.loading_primary_mol_kg,
           'pointer_before': current_pointer(config, task, attempt),
           'integrity_warnings': [x for x in integrity['issues'] if x['severity'] != 'ERROR']}
    return row, {'parsed_result.json': parsed.to_dict(), 'contract_report.json': contract,
                 'json_output_documents.json': {'documents': json_docs,
                  'comparison_status': 'NOT_EVALUATED_BY_P05', 'authoritative_source': 'text'},
                 'original_evidence_manifest.json': before}


def _prepare(config, ids, all_attempts):
    tasks = _tasks(config, ids)
    selected = []; excluded = []; payloads = {}; states = {}
    for task in tasks:
        st = load_status(config.root, task.task_id)
        states[task.task_id] = canonical_sha256(st)
        claim = within(task_directory(config.root, task.task_id), 'claims/active')
        if st.get('state') in ACTIVE or claim.exists():
            excluded.append({'task_id': task.task_id, 'reason': 'ACTIVE_TASK_OR_CLAIM'})
            if ids is not None:
                raise EvidenceError('Selected task is active; reparse refused')
            continue
        attempts = list_attempts(config, task)
        if not all_attempts:
            attempts = attempts[-1:]
        for attempt in attempts:
            row, payload = _calculate(config, task, attempt, st)
            selected.append(row); payloads[row['revision_id']] = (task, attempt, payload)
    plan = {'schema': 'rcm-reparse-plan-p05-v1', 'root': str(no_symlinks(config.root)),
            'campaign': config.name, 'config_sha256': config.config_sha256,
            'task_ids': ids, 'all_attempts': bool(all_attempts), 'task_status_hashes': states,
            'selected': selected, 'excluded': excluded, 'state_changes': False}
    return plan, payloads


def reparse_tasks(config, *, task_ids=None, all_attempts=False, apply=False, yes=False,
                  plan_file: Path | None = None, select=True):
    if not apply:
        plan, _ = _prepare(config, task_ids, all_attempts)
        return {'schema': 'rcm-reparse-preview-p05-v1', 'status': 'PREVIEW',
                'preview_id': canonical_sha256(plan), 'plan': plan,
                'selected_count': len(plan['selected']), 'reparsed': 0,
                'results': plan['selected'], 'production_executed': False}
    if not yes or plan_file is None:
        raise EvidenceError('Reparse apply requires --apply --yes --plan-file')
    saved = read_json_file(no_symlinks(plan_file))
    if saved.get('schema') != 'rcm-reparse-preview-p05-v1':
        raise EvidenceError('Not a reparse preview document')
    expected = saved.get('plan')
    if saved.get('preview_id') != canonical_sha256(expected):
        raise EvidenceError('Preview hash mismatch')
    if task_ids is not None or all_attempts:
        raise EvidenceError('Apply uses the saved selection; do not repeat selection flags')
    tasks = _tasks(config, expected.get('task_ids'))
    with task_locks(config, tasks):
        plan, payloads = _prepare(config, expected.get('task_ids'), expected.get('all_attempts', False))
        if canonical_sha256(plan) != saved['preview_id']:
            raise EvidenceError('Sources, policy, task state or selected revision changed after preview')
        if not plan['selected']:
            raise EvidenceError('No eligible attempts selected')
        records = []
        # Build and verify all new revisions before publishing any pointer.
        for row in plan['selected']:
            rid = row['revision_id']; task, attempt, files = payloads[rid]
            base = analysis_root(config, task, attempt); target = within(base, rid)
            reused = target.exists()
            if reused:
                meta = validate_revision(target)
                if meta['identity'] != row['identity']:
                    raise EvidenceError('Existing revision identity mismatch')
            else:
                base.mkdir(parents=True, exist_ok=True)
                pending = within(base, new_id('pending')); pending.mkdir(exist_ok=False)
                for name, value in files.items():
                    atomic_write_json(pending/name, value)
                meta = {'schema': 'rcm-derived-revision-p05-v1', 'identity': row['identity'],
                        'created_utc': utc_now(), 'previous_revision': row['pointer_before'],
                        'proposed_interpretation_state': row['proposed_interpretation_state'],
                        'task_state_modified': False, 'manifest': snapshot(pending)}
                atomic_write_json(pending/'revision.json', meta)
                # Tasks are locked; no cooperating writer can create this identity concurrently.
                if target.exists():
                    raise EvidenceError('Revision appeared during apply')
                os.rename(pending, target)
                validate_revision(target)
            records.append({'task_id': task.task_id, 'attempt_no': row['attempt_no'],
                            'revision_id': rid, 'metadata_sha256': digest_file(target/'revision.json')['sha256'],
                            'reused': reused, 'task_state_modified': False})
        for row in plan['selected']:
            task, attempt, _ = payloads[row['revision_id']]
            if (canonical_sha256(snapshot(attempt)) != row['identity']['attempt_snapshot_sha256']
                or canonical_sha256(load_status(config.root, task.task_id)) != plan['task_status_hashes'][task.task_id]
                or current_pointer(config, task, attempt) != row['pointer_before']):
                raise EvidenceError('Evidence/state/pointer changed before publish; revisions retained, not selected')
        for row, rec in zip(plan['selected'], records):
            if select:
                task, attempt, _ = payloads[row['revision_id']]
                pointer = {k: rec[k] for k in ('revision_id', 'metadata_sha256')}
                if pointer != row['pointer_before']:
                    atomic_write_json(within(analysis_root(config, task, attempt), 'current.json'), pointer)
        # Receipt is append-only and references every selected result. No original attempts changed.
        receipt_root = within(config.root, 'reports/reparse'); receipt_root.mkdir(parents=True, exist_ok=True)
        receipt = {'schema': 'rcm-reparse-receipt-p05-v1', 'status': 'PASS',
                   'preview_id': saved['preview_id'], 'created_utc': utc_now(),
                   'results': records, 'reparsed': len(records), 'selection_applied': bool(select),
                   'task_status_modified': False, 'production_executed': False,
                   'note': 'Selecting an interpretation does not authorize changing native task status.'}
        out = within(receipt_root, new_id('reparse')+'.json')
        atomic_write_json(out, receipt)
        return {**receipt, 'receipt_path': str(out)}
