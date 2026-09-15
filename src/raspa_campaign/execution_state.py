"""P06 task-local state journal and cooperative process claims (Linux).

Locks use the immutable task.json inode, shared with P05 evidence readers.
No lock file is created by previews. Claim age is never proof of a dead owner.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import socket
from typing import Any
import uuid

from .atomic import atomic_write_json
from .evidence import list_attempts, new_id, optional_json
from .hashing import canonical_sha256
from .models import TaskState, utc_now
from .paths import within, task_directory, read_json_file
from .tasks import load_status

STATES = {s.value for s in TaskState}
ACTIVE = {'CLAIMED', 'RUNNING'}
FAILURES = STATES - ACTIVE - {'PENDING', 'COMPLETE'}
RETRYABLE = {'FAILED', 'TIMEOUT', 'CANCELLED'}
ALLOWED = {
    'PENDING': {'CLAIMED'}, 'CLAIMED': {'CLAIMED','RUNNING','FAILED','CANCELLED'},
    'RUNNING': {'RUNNING'} | FAILURES | {'COMPLETE'},
    **{s: {'PENDING'} for s in RETRYABLE},
}

class StateError(ValueError): pass
class BusyError(StateError): pass

@contextmanager
def locks(config, tasks):
    """Nonblocking to avoid freezing the event loop behind another task reader."""
    fds=[]
    try:
        for task in sorted(tasks, key=lambda t: t.task_id):
            p=within(task_directory(config.root,task.task_id),'task.json')
            # NFS exclusive locks require write-capable descriptors; never write/truncate this file.
            fd=os.open(p,os.O_RDWR|getattr(os,'O_NOFOLLOW',0));fds.append(fd)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:raise BusyError('Task is busy: '+task.task_id) from exc
        yield
    finally:
        for fd in reversed(fds):fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def proc_identity(pid):
    if type(pid) is not int or pid <= 0:return None
    try:
        text=Path(f'/proc/{pid}/stat').read_text()
        fields=text[text.rfind(')')+2:].split()
        return {'pid':pid,'state':fields[0],'ppid':int(fields[1]),'pgrp':int(fields[2]),
                'session':int(fields[3]),'start_ticks':int(fields[19])}
    except FileNotFoundError:return None
    except (OSError,ValueError,IndexError) as exc:raise StateError('Cannot inspect process identity') from exc


def host_identity():
    return {'hostname':socket.gethostname(),
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def owner_record(task_id,run_id):
    ident=proc_identity(os.getpid())
    return {'schema':'rcm-claim-p06-v1','claim_id':'claim_'+uuid.uuid4().hex,
            'task_id':task_id,'run_id':run_id,**host_identity(),'manager_pid':os.getpid(),
            'manager_start_ticks':ident['start_ticks'],'created_utc':utc_now(),
            'pbs_job_id':os.environ.get('PBS_JOBID',''),
            'pbs_array_index':os.environ.get('PBS_ARRAY_INDEX',os.environ.get('PBS_ARRAYID',''))}


def claim_path(config,task):return within(task_directory(config.root,task.task_id),'claims/active')


def claim_snapshot(config,task):
    p=claim_path(config,task)
    if not p.exists():return None
    if not p.is_dir():raise StateError('Active claim is not a directory')
    owner=optional_json(within(p,'owner.json'))
    beat=optional_json(within(p,'heartbeat.json'))
    return {'owner':owner,'heartbeat':beat,
            'fingerprint':canonical_sha256({'owner':owner,'heartbeat':beat})}


def owner_liveness(snap):
    if snap is None:return 'NO_CLAIM'
    o=snap.get('owner')
    if not isinstance(o,dict) or o.get('schema')!='rcm-claim-p06-v1':return 'OWNER_MISSING_OR_UNKNOWN'
    host=host_identity()
    if o.get('hostname')!=host['hostname']:return 'REMOTE_OWNER_NEEDS_SCHEDULER_EVIDENCE'
    if o.get('boot_id')!=host['boot_id']:return 'PREVIOUS_BOOT'
    p=proc_identity(o.get('manager_pid'))
    if p and p['state'] not in {'Z','X'} and p['start_ticks']==o.get('manager_start_ticks'):return 'ALIVE'
    return 'LOCAL_OWNER_NOT_ALIVE'


def create_claim(config,task,run_id):
    p=claim_path(config,task)
    try:p.mkdir()
    except FileExistsError as exc:raise BusyError('Claim already exists: '+task.task_id) from exc
    record=owner_record(task.task_id,run_id)
    # If this write fails the empty claim remains: never silently delete it.
    atomic_write_json(p/'owner.json',record)
    return record


def heartbeat(config,task,owner,*,native=None,sequence=1):
    p=claim_path(config,task)
    if optional_json(p/'owner.json')!=owner:raise StateError('Claim ownership changed')
    atomic_write_json(p/'heartbeat.json',{'claim_id':owner['claim_id'],'sequence':sequence,
        'updated_utc':utc_now(),'native':native})


def release_claim(config,task,owner,reason):
    p=claim_path(config,task)
    if optional_json(p/'owner.json')!=owner:raise StateError('Cannot release a different claim')
    history=within(task_directory(config.root,task.task_id),'claims/history');history.mkdir(exist_ok=True)
    dest=within(history,owner['claim_id'])
    if dest.exists():raise StateError('Claim history collision')
    atomic_write_json(p/'release.json',{'claim_id':owner['claim_id'],'reason':reason,'released_utc':utc_now()})
    os.rename(p,dest)  # preserve owner/heartbeat/release evidence, do not delete recursively


def audit_journal(config,task):
    st=load_status(config.root,task.task_id)
    folder=within(task_directory(config.root,task.task_id),'events')
    paths=sorted(folder.glob('*.json')) if folder.exists() else []
    if not paths:
        if st.get('revision',0) or st.get('last_event_id'):raise StateError('Status journal is missing')
        return {'status':'LEGACY_NO_JOURNAL','event_count':0}
    last=None
    for n,p in enumerate(paths,1):
        within(folder,p.name);e=read_json_file(p)
        if e.get('schema')!='rcm-state-event-p06-v1' or e.get('sequence')!=n or e.get('task_id')!=task.task_id:
            raise StateError('Invalid task event sequence/identity')
        if p.name!=f'{n:08d}_{e.get("event_id")}.json':raise StateError('Event filename identity mismatch')
        if e['event_id']!='evt_'+canonical_sha256({k:v for k,v in e.items() if k!='event_id'}):
            raise StateError('Event digest mismatch')
        if e['after'].get('revision')!=n or e['after']['state'] not in ALLOWED.get(e['before']['state'],set()):
            raise StateError('Illegal recorded transition')
        if last and e['before']!=last:raise StateError('State event chain has a gap')
        last={**e['after'],'last_event_id':e['event_id']}
    if last!=st:raise StateError('State snapshot and event journal disagree; recovery review required')
    return {'status':'PASS','event_count':len(paths)}


def write_status(config,task,**updates):
    """Caller holds task lock. Event first, then snapshot; torn commit fails audit."""
    old=load_status(config.root,task.task_id);audit_journal(config,task)
    state=updates.get('state',old.get('state'))
    if state not in ALLOWED.get(old.get('state'),set()):raise StateError(f'Illegal transition: {old.get("state")} -> {state}')
    rev=old.get('revision',0)
    if type(rev) is not int or rev < 0:raise StateError('Invalid status revision')
    new={**old,**updates,'updated_utc':utc_now(),'revision':rev+1}
    new.pop('last_event_id',None)
    event={'schema':'rcm-state-event-p06-v1','task_id':task.task_id,'sequence':rev+1,
           'before':old,'after':new,'actor':{'hostname':socket.gethostname(),'pid':os.getpid()}}
    event['event_id']='evt_'+canonical_sha256(event)
    folder=within(task_directory(config.root,task.task_id),'events');folder.mkdir(exist_ok=True)
    p=within(folder,f'{rev+1:08d}_{event["event_id"]}.json')
    if p.exists():raise StateError('Uncommitted event already exists')
    atomic_write_json(p,event)
    new={**new,'last_event_id':event['event_id']}
    atomic_write_json(within(task_directory(config.root,task.task_id),'status.json'),new)
    return new


def eligibility(config,task):
    st=load_status(config.root,task.task_id)
    if st.get('state') not in STATES:return 'UNKNOWN_STATE'
    # Do not rescan sealed bytes in a preview; audit is an explicit operation.
    audit_journal(config,task)
    nums=[int(p.name.split('_')[1]) for p in list_attempts(config,task)]
    no=st.get('attempt_no',0)
    if type(no) is not int or nums!=list(range(1,max(nums,default=0)+1)) or no!=max(nums,default=0):
        return 'ATTEMPT_HISTORY_MISMATCH'
    if claim_path(config,task).exists():return 'CLAIMED_ELSEWHERE'
    if st['state']=='COMPLETE':return 'ALREADY_COMPLETE'
    if st['state'] in ACTIVE:return 'ORPHAN_REVIEW_REQUIRED'
    if st['state']!='PENDING':return 'RETRY_APPROVAL_REQUIRED'
    if no>=config.get('execution.max_attempts',3):return 'MAX_ATTEMPTS'
    return 'ELIGIBLE'
