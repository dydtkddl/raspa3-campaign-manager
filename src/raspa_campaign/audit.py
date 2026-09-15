"""P05 read-only evidence audit. Execution failure and file integrity are separate."""
from __future__ import annotations
from pathlib import Path
from typing import Any

from .config import CampaignConfig
from .evidence import (ACTIVE, EvidenceError, analysis_root, attempt_number, attempt_observation,
    current_pointer, inspect_attempt, issue, list_attempts, optional_json, selected_view,
    validate_revision, digest_file)
from .models import utc_now
from .paths import within, task_directory
from .tasks import iter_tasks, load_status, load_task


def audit_campaign(config: CampaignConfig, *, require_all_complete=False, mode='safe', scope='all-attempts') -> dict[str, Any]:
    if mode not in {'safe', 'fast'} or scope not in {'all-attempts', 'latest', 'structure'}:
        raise EvidenceError('audit mode/scope is invalid')
    problems = []; counts = {}; hashed = unverified = checked_attempts = 0
    try:
        tasks = list(iter_tasks(config.root))
        # Include earlier task directories retained after a new input build.
        root = within(config.root, 'tasks')
        known = {t.task_id for t in tasks}
        if root.exists():
            for p in root.iterdir():
                if p.name.startswith('task_') and p.name not in known:
                    tasks.append(load_task(config.root, p.name))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return {'schema': 'rcm-audit-p05-v1', 'status': 'ERROR', 'error_count': 1,
                'issues': [issue('TASK_INDEX_READ_ERROR', observed=str(exc))], 'read_only': True}
    for task in tasks:
        try:
            st = load_status(config.root, task.task_id)
            from .execution_state import audit_journal
            audit_journal(config, task)
            state = str(st.get('state', 'UNKNOWN')); counts[state] = counts.get(state, 0) + 1
            if require_all_complete and state != 'COMPLETE':
                problems.append({**issue('TASK_NOT_COMPLETE', observed=state), 'task_id': task.task_id})
            attempts = list_attempts(config, task)
            nums = [attempt_number(p) for p in attempts]
            if nums != list(range(1, max(nums, default=0)+1)):
                problems.append({**issue('ATTEMPT_NUMBER_GAP', observed=nums), 'task_id': task.task_id})
            recorded = st.get('attempt_no', 0)
            if (type(recorded) is not int or recorded != max(nums, default=0)):
                problems.append({**issue('STATUS_ATTEMPT_MISMATCH', expected=max(nums, default=0), observed=recorded), 'task_id': task.task_id})
            if state == 'COMPLETE' and not attempts:
                problems.append({**issue('COMPLETE_WITHOUT_ATTEMPT'), 'task_id': task.task_id})
            selected = attempts[-1:] if scope == 'latest' else attempts
            for attempt in selected:
                no = attempt_number(attempt)
                claim = within(task_directory(config.root, task.task_id), 'claims/active')
                is_active = no == recorded and (state in ACTIVE or claim.exists())
                if is_active:
                    problems.append({**issue('ACTIVE_UNSEALED', severity='WARNING'), 'task_id': task.task_id, 'attempt': no})
                    continue
                if scope == 'structure':
                    problems.append({**issue('CONTENT_NOT_CHECKED', severity='WARNING'), 'task_id': task.task_id, 'attempt': no})
                    continue
                report = inspect_attempt(attempt, fast=(mode == 'fast'))
                checked_attempts += 1; hashed += report['hashed_files']; unverified += report['unverified_files']
                problems.extend({**x, 'task_id': task.task_id, 'attempt': no} for x in report['issues'])
                if report['error_count']:
                    continue
                observation = attempt_observation(attempt, st)
                if observation.get('task_id') not in (None, task.task_id):
                    raise EvidenceError('Attempt record belongs to another task')
                if task.input_contract and (attempt/'derived/staging_manifest.json').is_file():
                    from .templates import verify_staged_inputs
                    staged = optional_json(attempt/'derived/staging_manifest.json')
                    verify_staged_inputs(task, attempt, staged)
                if (no == recorded and observation.get('schema') == 'rcm-attempt-result-p05-v1'
                        and state not in {'PENDING', 'UNKNOWN'} and state != observation.get('state')):
                    problems.append({**issue('LATEST_STATUS_RECORD_MISMATCH', expected=observation.get('state'), observed=state),
                                     'task_id': task.task_id, 'attempt': no})
                if no == recorded and state == 'COMPLETE' and observation.get('state') != 'COMPLETE':
                    problems.append({**issue('COMPLETE_WITH_NONCOMPLETE_ATTEMPT', observed=observation.get('state')),
                                     'task_id': task.task_id, 'attempt': no})
                view = selected_view(config, task, attempt, integrity_report=report) if mode != 'fast' else None
                if state == 'COMPLETE' and no == recorded and view:
                    if view['contract'].get('status') != 'PASS':
                        problems.append({**issue('COMPLETE_WITH_FAILED_SELECTED_CONTRACT'), 'task_id': task.task_id, 'attempt': no})
                analysis = analysis_root(config, task, attempt)
                if analysis.exists():
                    for child in sorted(analysis.iterdir()):
                        within(analysis, child.name)
                        if child.name == 'current.json':
                            continue
                        if child.name.startswith('pending_'):
                            problems.append({**issue('UNPUBLISHED_REVISION', child, severity='WARNING'), 'task_id': task.task_id})
                            continue
                        if child.name.startswith('rev_'):
                            meta = validate_revision(child)
                            if meta['identity'].get('task_id') != task.task_id or meta['identity'].get('attempt_no') != no:
                                raise EvidenceError('Revision has wrong task/attempt identity')
                        else:
                            problems.append({**issue('UNEXPECTED_ANALYSIS_ENTRY', child), 'task_id': task.task_id})
        except (ValueError, OSError, TypeError, KeyError) as exc:
            problems.append({**issue('TASK_EVIDENCE_ERROR', observed=str(exc)), 'task_id': task.task_id})
    # Integrity and scientific success are distinct; require_all_complete is opt-in.
    errors = sum(p['severity'] == 'ERROR' for p in problems)
    return {'schema': 'rcm-audit-p05-v1', 'campaign': config.name, 'created_utc': utc_now(),
            'status': 'FAIL' if errors else 'WARN' if problems else 'PASS',
            'checked_tasks': len(tasks), 'checked_attempts': checked_attempts,
            'state_counts': counts, 'issue_count': len(problems), 'error_count': errors,
            'issues': problems, 'mode': mode, 'scope': scope, 'hashed_files': hashed,
            'unverified_files': unverified, 'read_only': True,
            'event_transition_audit': 'P06_JOURNAL_CHECKED_WHERE_PRESENT; LEGACY_NO_JOURNAL_NOT_RECONSTRUCTED',
            'trust_boundary': 'Hashes check consistency against recorded manifests; not a signed authenticity proof.'}
