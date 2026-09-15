"""P05 verified gzip copies, outside sealed native attempts. Never delete raw."""
from __future__ import annotations
import gzip
import hashlib
import os
import shutil
from pathlib import Path

from .atomic import atomic_write_json
from .evidence import (ACTIVE, EvidenceError, attempt_observation, digest_file, list_attempts,
    new_id, require_integrity, snapshot, task_locks)
from .paths import within, task_directory
from .tasks import iter_tasks, load_status


def compress_completed_outputs(config, *, delete_raw=False, yes=False):
    if delete_raw:
        raise EvidenceError('Raw deletion is disabled: it would invalidate immutable attempt evidence')
    tasks = list(iter_tasks(config.root)); sources = []
    with task_locks(config, tasks):
        for task in tasks:
            st = load_status(config.root, task.task_id)
            if st.get('state') in ACTIVE or within(task_directory(config.root,task.task_id),'claims/active').exists():
                continue
            for attempt in list_attempts(config, task):
                if attempt_observation(attempt,st).get('state') != 'COMPLETE':
                    continue
                require_integrity(attempt)
                for r in snapshot(attempt):
                    if (r['relative_path'].startswith('work/output/') and r['relative_path'].endswith('.txt')) or r['relative_path'] in {'raw/stdout.log','raw/stderr.log'}:
                        sources.append({'task_id':task.task_id,'attempt':attempt.name,**r})
        if not yes:
            return {'status':'PREVIEW','files':sources,'source_files_deleted':False,'apply_command':'compress-logs --yes'}
        out = within(config.root,'archives/'+new_id('logs'));out.mkdir(parents=True,exist_ok=False)
        records=[]
        for r in sources:
            src=within(task_directory(config.root,r['task_id']),'attempts/'+r['attempt']+'/'+r['relative_path'])
            if digest_file(src)['sha256'] != r['sha256']:
                raise EvidenceError('Raw log changed after archive selection')
            target=within(out,r['task_id']+'/'+r['attempt']+'/'+r['relative_path']+'.gz');target.parent.mkdir(parents=True,exist_ok=True)
            with src.open('rb') as inp,target.open('xb') as binary:
                with gzip.GzipFile(filename='',mode='wb',fileobj=binary,mtime=0) as zipped:
                    shutil.copyfileobj(inp,zipped)
            h=hashlib.sha256()
            with gzip.open(target,'rb') as inp:
                for block in iter(lambda:inp.read(1024*1024),b''):h.update(block)
            if h.hexdigest()!=r['sha256'] or digest_file(src)['sha256']!=r['sha256']:
                raise EvidenceError('Compressed log roundtrip/source hash mismatch')
            records.append({**r,'compressed_relative_path':target.relative_to(out).as_posix(),
                            'compressed_sha256':digest_file(target)['sha256'],'roundtrip_verified':True})
        report={'status':'PASS','output':str(out),'compressed_count':len(records),'source_files_deleted':False,'records':records}
        atomic_write_json(out/'archive_manifest.json',report)
        return report
