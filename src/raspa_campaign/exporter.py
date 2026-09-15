"""P05 immutable, checked exports with explicit attempt/revision provenance."""
from __future__ import annotations
import csv
import json
import math
import os
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .evidence import (ACTIVE, EvidenceError, attempt_number, attempt_observation, classify,
    current_pointer, digest_file, list_attempts, new_id, optional_json, selected_view,
    snapshot, task_locks, verify_manifest)
from .hashing import canonical_sha256
from .models import utc_now
from .paths import within, no_symlinks, read_json_file, read_regular, task_directory
from .tasks import iter_tasks, load_status

TABLES = ['runs', 'final_loadings', 'cycle_status', 'energy_statistics', 'move_statistics',
          'runtime_contract', 'runtime_resources', 'events', 'files']
BASE = ['campaign', 'task_id', 'mof_id', 'gas', 'temperature_K', 'pressure_bar', 'pressure_Pa',
        'seed', 'replicate', 'model_branch', 'attempt', 'attempt_no', 'derived_revision', 'authoritative']
FIELDS = {
 'runs': ['state', 'task_current_state', 'exit_code', 'external_wall_seconds', 'loading_primary_mol_kg',
          'uncertainty_primary_mol_kg', 'last_cycle', 'parse_status', 'contract_status', 'hostname',
          'pbs_job_id', 'engine_binary', 'engine_binary_sha256', 'selection_reason'],
 'final_loadings': ['loading_kind', 'unit', 'value', 'uncertainty', 'cycle', 'record_role', 'source', 'line_no'],
 'cycle_status': ['cycle', 'phase', 'metric', 'value', 'unit', 'source', 'line_no'],
 'energy_statistics': ['energy_term', 'value', 'uncertainty', 'unit', 'cycle', 'source', 'line_no'],
 'move_statistics': ['move_type', 'cycle', 'source', 'line_no'],
 'runtime_contract': ['field', 'reported_value', 'expected_value', 'match_status'],
 'runtime_resources': ['exit_code', 'external_wall_seconds', 'hostname', 'pbs_job_id'],
 'events': ['event_type', 'severity', 'message', 'source', 'line_no'],
 'files': ['relative_path', 'sha256', 'size_bytes', 'role']}
LEGACY = ['name', 'gas', 'temp', 'pressure', 'cutoff', 'path', 'abs_mol_per_kg_framework',
          'abs_molecules_per_uc', 'abs_mg_per_g_framework', 'abs_cm3STP_per_g', 'abs_cm3STP_per_cm3']


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _scalarize(value):
    if isinstance(value, (dict, list)):
        return json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, allow_nan=False)
    return _clean(value)


def _write_csv(path, rows, fields=None):
    prefix = fields or []
    names = prefix + sorted({k for r in rows for k in r} - set(prefix))
    with path.open('x', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=names, lineterminator='\n', extrasaction='raise')
        w.writeheader()
        for row in rows:
            w.writerow({k: _scalarize(v) for k, v in row.items()})
        fh.flush(); os.fsync(fh.fileno())
    return names


def _parquet(path, rows, fields):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return {'status': 'UNAVAILABLE', 'message': 'pyarrow not installed'}
    try:
        data = [{k: _scalarize(r.get(k)) for k in fields} for r in rows]
        table = pa.Table.from_pylist(data) if data else pa.table({k: pa.array([], type=pa.string()) for k in fields})
        pq.write_table(table, path, compression='zstd')
        return {'status': 'PASS', 'rows': len(rows)}
    except Exception as exc:
        return {'status': 'FAIL', 'message': type(exc).__name__ + ': ' + str(exc)}


def _attempt_rows(config, task, attempt, *, st=None, authoritative=False, selection_reason='diagnostic'):
    st = st or load_status(config.root, task.task_id)
    view = selected_view(config, task, attempt)
    parsed, contract = view['parsed'], view['contract']
    obs = attempt_observation(attempt, st)
    base = {'campaign': config.name, 'task_id': task.task_id, 'mof_id': task.mof_id, 'gas': task.gas,
            'temperature_K': task.temperature_K, 'pressure_bar': task.pressure_bar, 'pressure_Pa': task.pressure_Pa,
            'seed': task.seed, 'replicate': task.replicate, 'model_branch': task.model_branch,
            'attempt': attempt.name, 'attempt_no': attempt_number(attempt), 'derived_revision': view['revision_id'],
            'authoritative': authoritative}
    resources = optional_json(within(attempt, 'derived/resource_usage.json'), {})
    launch = optional_json(within(attempt, 'derived/launch_contract.json'), {})
    run = {**base, 'state': obs.get('state'), 'task_current_state': st.get('state'),
           'exit_code': obs.get('exit_code'), 'external_wall_seconds': obs.get('external_wall_seconds'),
           'selection_reason': selection_reason, 'state_source': obs.get('state_source', 'attempt_result'),
           'hostname': resources.get('hostname'), 'pbs_job_id': resources.get('pbs_job_id'),
           'engine_binary': launch.get('engine_binary'), 'engine_binary_sha256': launch.get('engine_binary_sha256'),
           'contract_status': contract.get('status'), 'interpretation_state': (obs.get('state') if obs.get('state') in {'TIMEOUT','CANCELLED'} else classify(obs.get('exit_code'), parsed, contract))}
    run.update({k: parsed.get(k) for k in ('loading_primary_mol_kg','uncertainty_primary_mol_kg','last_cycle','parse_status')})
    def rows(key):
        return [{**base, **r} for r in parsed.get(key, [])]
    out = {'runs': [run], 'final_loadings': rows('final_loadings'), 'cycle_status': rows('cycle_status'),
           'energy_statistics': rows('energy_statistics'), 'move_statistics': rows('move_statistics'),
           'runtime_contract': rows('runtime_contract'), 'runtime_resources': [{**base, **resources}] + rows('native_timings'),
           'events': rows('events') + rows('unknown_sections'),
           'files': [{**base, **r} for r in snapshot(attempt)]}
    for check in contract.get('checks', []):
        out['runtime_contract'].append({**base, 'field': 'contract_check:' + str(check.get('name')),
             'reported_value': check.get('observed'), 'expected_value': check.get('expected'),
             'match_status': 'PASS' if check.get('passed') else 'FAIL'})
    return out, view


def _legacy_row(config, task, attempt, parsed):
    # Preserve legacy column names; read observed values without guessed STP conversion.
    row = dict.fromkeys(LEGACY)
    row.update(name=task.mof_id, gas=task.gas, temp=task.temperature_K, pressure=task.pressure_bar, path=str(attempt/'work/output'))
    ff = optional_json(within(attempt, 'work/force_field.json'), {})
    sim = optional_json(within(attempt, 'work/simulation.json'), {})
    systems = sim.get('Systems') or [{}]
    row['cutoff'] = systems[0].get('CutOffVDW', ff.get('CutOffVDW'))
    mapping = {'mol_kg_framework': 'abs_mol_per_kg_framework', 'molecules_uc': 'abs_molecules_per_uc',
               'mg_g_framework': 'abs_mg_per_g_framework'}
    for unit, col in mapping.items():
        matches = [r for r in parsed.get('final_loadings', []) if r.get('loading_kind') == 'absolute'
                   and r.get('unit') == unit and r.get('record_role') == 'final_mean'
                   and r.get('source') == parsed.get('final_cycle_source')
                   and (r.get('line_no') or 0) > (parsed.get('final_cycle_line_no') or 10**15)]
        if len(matches) > 1:
            raise EvidenceError('Ambiguous final loading for legacy compatibility column: ' + col)
        row[col] = matches[0]['value'] if matches else None
    return row


def verify_export(path):
    manifest = read_json_file(within(path, 'file_manifest.json'))
    issues, checked, _ = verify_manifest(path, manifest, exclude={'file_manifest.json'})
    if issues:
        raise EvidenceError('Export manifest mismatch: ' + json.dumps(issues))
    return {'status': 'PASS', 'files_checked': checked}


def latest_export_dir(config):
    root = within(config.root, 'exports')
    pointer = optional_json(within(root, 'latest.json'))
    if pointer:
        path = within(root, pointer['directory'])
        if digest_file(path/'file_manifest.json')['sha256'] != pointer['file_manifest_sha256']:
            raise EvidenceError('Latest export pointer hash mismatch')
        verify_export(path)
        return path
    # Read-only fallback for previous exports. Only an internal resolved link is accepted.
    old = root/'latest'
    if old.is_symlink():
        target = old.resolve(strict=True)
        if not target.is_relative_to(root) or target == root:
            raise EvidenceError('Legacy latest export escapes exports root')
        no_symlinks(target)
        return target
    no_symlinks(old)
    return old


def export_campaign(config, *, all_attempts=False, parquet=False, require_parquet=False,
                    task_ids=None, states=None, gases=None, metadata_csv=None, metadata_id_column='filename'):
    if require_parquet and not parquet:
        raise EvidenceError('--require-parquet requires --parquet')
    tasks = sorted(iter_tasks(config.root), key=lambda t: t.task_id)
    if task_ids:
        if set(task_ids) - {t.task_id for t in tasks}:
            raise EvidenceError('Unknown task ID in export filter')
        tasks = [t for t in tasks if t.task_id in task_ids]
    from .models import TaskState
    if states and set(states) - {s.value for s in TaskState}:
        raise EvidenceError('Invalid state filter')
    if gases and set(gases) - {t.gas for t in tasks}:
        raise EvidenceError('Unknown gas filter')
    query = {'task_ids': task_ids, 'states': sorted(states or []), 'gases': sorted(gases or []),
             'all_attempts': bool(all_attempts)}
    metadata = {}; metadata_ref = None
    if metadata_csv:
        mpath = no_symlinks(Path(metadata_csv)); raw = read_regular(mpath).decode('utf-8-sig')
        import io
        reader = csv.DictReader(io.StringIO(raw))
        if metadata_id_column not in (reader.fieldnames or []) or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise EvidenceError('Metadata ID column missing or duplicate header')
        for r in reader:
            k = r.get(metadata_id_column)
            if not k or k in metadata:
                raise EvidenceError('Metadata IDs must be nonempty and unique')
            metadata[k] = r
        metadata_ref = {'path': str(mpath), **digest_file(mpath), 'id_column': metadata_id_column}
    with task_locks(config, tasks):
        tables = {t: [] for t in TABLES}; legacy = []; times = []; skipped = []; refs = []
        for task in tasks:
            st = load_status(config.root, task.task_id)
            if (states and st.get('state') not in states) or (gases and task.gas not in gases):
                continue
            attempts = list_attempts(config, task)
            active = st.get('state') in ACTIVE or within(task_directory(config.root, task.task_id), 'claims/active').exists()
            sealed = []
            for p in attempts:
                if active and attempt_number(p) == st.get('attempt_no'):
                    skipped.append({'task_id': task.task_id, 'attempt': p.name, 'reason': 'ACTIVE_UNSEALED'}); continue
                if not (p/'derived/file_manifest.json').is_file():
                    skipped.append({'task_id': task.task_id, 'attempt': p.name, 'reason': 'UNSEALED_NO_MANIFEST'})
                    if st.get('state') == 'COMPLETE' and attempt_number(p) == st.get('attempt_no'):
                        raise EvidenceError('Complete task has no sealed evidence')
                    continue
                selected_view(config, task, p)  # Do not silently omit corrupt attempts, even when selecting one.
                sealed.append(p)
            complete = [p for p in sealed if attempt_observation(p, st).get('state') == 'COMPLETE'
                        and selected_view(config, task, p)['contract'].get('status') == 'PASS']
            authoritative = complete[-1] if complete else None
            explicit = st.get('authoritative_attempt')
            if explicit is not None:
                authoritative = next((p for p in complete if attempt_number(p) == explicit), None)
                if authoritative is None:
                    raise EvidenceError('authoritative_attempt points to a noncomplete/missing attempt')
            chosen = sealed if all_attempts else [authoritative] if authoritative else sealed[-1:]
            for attempt in chosen:
                before = snapshot(attempt); pointer = current_pointer(config, task, attempt)
                auth = attempt == authoritative
                data, view = _attempt_rows(config, task, attempt, st=st, authoritative=auth,
                    selection_reason='authoritative complete' if auth else 'sealed diagnostic attempt')
                for table in TABLES:
                    tables[table].extend(data[table])
                refs.append({'task_id': task.task_id, 'attempt': attempt.name, 'attempt_snapshot_sha256': canonical_sha256(before),
                    'task_status_sha256': canonical_sha256(st), 'derived_revision': view['revision_id'],
                    'revision_metadata_sha256': view['revision_metadata_sha256'], 'pointer': pointer})
                if auth:
                    legacy.append(_legacy_row(config, task, attempt, view['parsed']))
                    tr = {'MOF': task.mof_id, 'gas': task.gas, 'temp': task.temperature_K, 'pressure': task.pressure_bar,
                          'seed': task.seed, 'replicate': task.replicate, 'task_id': task.task_id,
                          'attempt': attempt.name, 'time (s)': data['runs'][0]['external_wall_seconds']}
                    if metadata_csv:
                        tr['metadata_matched'] = task.mof_id in metadata
                        tr.update({'meta__'+k: v for k,v in metadata.get(task.mof_id, {}).items()})
                    times.append(tr)
        # Validate snapshot again before writing, and again immediately before latest-pointer publication.
        def recheck():
            by_id = {t.task_id: t for t in tasks}
            for ref in refs:
                task = by_id[ref['task_id']]
                attempt = within(task_directory(config.root, task.task_id), 'attempts/' + ref['attempt'])
                if (canonical_sha256(snapshot(attempt)) != ref['attempt_snapshot_sha256']
                    or canonical_sha256(load_status(config.root, task.task_id)) != ref['task_status_sha256']
                    or current_pointer(config, task, attempt) != ref['pointer']):
                    raise EvidenceError('Evidence/state/revision changed during export')
                selected_view(config, task, attempt)
            if metadata_ref and digest_file(Path(metadata_ref['path']))['sha256'] != metadata_ref['sha256']:
                raise EvidenceError('Metadata changed during export')
        recheck()
        filtered = bool(task_ids or states or gases)
        outroot = within(config.root, 'exports'); outroot.mkdir(parents=True, exist_ok=True)
        eid = new_id('export_FILTERED' if filtered else 'export')
        target = within(outroot, eid); pending = within(outroot, '.pending_'+eid); pending.mkdir(exist_ok=False)
        schemas = {}; parquet_status = {}; nonfinite = []
        for name, rows in tables.items():
            for i, r in enumerate(rows):
                for k,v in r.items():
                    if isinstance(v, float) and not math.isfinite(v):
                        nonfinite.append({'table': name, 'row': i, 'column': k, 'raw': repr(v)})
            fields = _write_csv(pending/(name+'.csv'), rows, BASE+FIELDS[name]); schemas[name] = fields
            if parquet:
                parquet_status[name] = _parquet(pending/(name+'.parquet'), rows, fields)
        schemas['07result'] = _write_csv(pending/'07result.csv', legacy, LEGACY)
        schemas['7_time'] = _write_csv(pending/'7_time.csv', times,
                ['MOF','gas','temp','pressure','seed','replicate','task_id','attempt','time (s)'])
        # Same raw observations; no guessed STP units or gas-name slicing, and no metadata inner join.
        atomic_write_json(pending/'compatibility.json', {'legacy_result_columns': LEGACY,
             'rows': len(legacy), 'selection': 'authoritative COMPLETE only',
             'abs_cm3STP_per_g': 'not emitted by current native parser; blank, never synthesized',
             'abs_cm3STP_per_cm3': 'not emitted by current native parser; blank, never synthesized',
             'time_table': 'stable time (s) column replaces directory-dependent column name',
             'Henry': 'not implemented by P05', 'metadata_join': 'left/exact MOF ID; no discarded unmatched rows'})
        badformat = any(r['status'] != 'PASS' for r in parquet_status.values())
        status = 'FAIL' if require_parquet and badformat else 'WARN' if badformat or skipped or nonfinite else 'PASS'
        manifest = {'schema': 'rcm-export-p05-v1', 'status': status, 'campaign': config.name,
             'created_utc': utc_now(), 'all_attempts': bool(all_attempts), 'query': query, 'filtered': filtered,
             'source_refs': refs, 'excluded_attempts': skipped, 'metadata': metadata_ref,
             'row_counts': {**{k: len(v) for k,v in tables.items()}, '07result': len(legacy), '7_time': len(times)},
             'columns': schemas, 'nonfinite_cells_as_blank': nonfinite, 'parquet': parquet_status,
             'csv_status': 'PASS', 'require_parquet': bool(require_parquet)}
        atomic_write_json(pending/'export_manifest.json', manifest)
        atomic_write_json(pending/'file_manifest.json', snapshot(pending))
        verify_export(pending); recheck()
        if target.exists():
            raise EvidenceError('Unexpected export ID collision; old exports preserved')
        os.rename(pending, target)
        # A requested required format failure preserves old latest and the diagnostic export.
        if status != 'FAIL':
            atomic_write_json(within(outroot, 'latest.json'), {'schema': 'rcm-latest-export-p05-v1',
                'directory': eid, 'file_manifest_sha256': digest_file(target/'file_manifest.json')['sha256']})
        return {'output': str(target), **manifest, 'latest_updated': status != 'FAIL'}
