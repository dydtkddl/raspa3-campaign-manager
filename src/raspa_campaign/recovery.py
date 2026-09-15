"""P06 preview-first retry, drain and conservative claim inspection/release."""
from __future__ import annotations
import os
from pathlib import Path
from .atomic import atomic_write_json
from .config import CampaignConfig
from .evidence import (new_id,optional_json,list_attempts,require_integrity,attempt_observation,snapshot)
from .hashing import canonical_sha256
from .models import utc_now
from .paths import task_directory,within,read_json_file,validate_task_id
from .tasks import iter_tasks,load_status,load_task
from .input_identity import verify_task_inputs
from .execution_state import (locks,RETRYABLE,STATES,ACTIVE,StateError,claim_path,claim_snapshot,
    owner_liveness,proc_identity,write_status,audit_journal,host_identity)


def _plan(kind,config,payload):
    ident={'kind':kind,'campaign_root':str(config.root),'config_sha256':config.config_sha256,**payload}
    return {'schema':'rcm-action-plan-p06-v1','status':'PREVIEW','plan_id':'plan_'+canonical_sha256(ident),
            'identity':ident,'created_utc':utc_now(),'mutations_performed':False}


def _read_plan(path,kind,config):
    if path is None:raise ValueError('Review preview JSON; --plan-file is required with --apply --yes')
    p=read_json_file(Path(path))
    if (p.get('schema')!='rcm-action-plan-p06-v1' or p.get('plan_id')!='plan_'+canonical_sha256(p.get('identity'))
        or p['identity'].get('kind')!=kind or p['identity'].get('campaign_root')!=str(config.root)
        or p['identity'].get('config_sha256')!=config.config_sha256):raise ValueError('Invalid/stale action plan')
    return p


def _approved(apply,yes):
    if apply and not yes:raise PermissionError('--apply requires --yes; no interactive prompt')


def retry_tasks(config,*,task_ids=None,states=None,gases=None,chunk_id=None,reason='manual review',
                apply=False,yes=False,plan_file=None,max_selected=1000):
    _approved(apply,yes)
    if apply:
        saved=_read_plan(plan_file,'retry',config);filters=saved['identity']['filters']
        task_ids=filters['task_ids'];states=filters['states'];gases=filters['gases'];chunk_id=filters['chunk_id']
        max_selected=filters['max_selected'];reason=saved['identity']['reason']
    if type(max_selected) is not int or max_selected<1:raise ValueError('max_selected must be a positive integer')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('An explicit nonempty reason is required')
    if states and set(states)-STATES:raise ValueError('Unknown retry state')
    index=list(iter_tasks(config.root));known={t.task_id:t for t in index}
    if chunk_id is not None:
        from .scheduler import load_planned_tasks
        chunk_members={t.task_id for t in load_planned_tasks(config,chunk_id=chunk_id)}
    else:chunk_members=None
    if task_ids:
        for tid in task_ids:
            validate_task_id(tid)
            if tid not in known:raise ValueError('Unknown selected task: '+tid)
    filters={'task_ids':sorted(set(task_ids)) if task_ids else None,'states':sorted(set(states)) if states else None,
             'gases':sorted(set(gases)) if gases else None,'chunk_id':chunk_id,'max_selected':max_selected}
    relevant=[t for t in index if (not task_ids or t.task_id in task_ids) and (not gases or t.gas in gases)
              and (chunk_members is None or t.task_id in chunk_members)]
    def prepare():
        chosen=[];excluded=[]
        for t in relevant:
            st=load_status(config.root,t.task_id);state=st['state']
            if states and state not in states:continue
            why=None
            if claim_path(config,t).exists() or state in ACTIVE:why='ACTIVE_OR_CLAIMED'
            elif state not in RETRYABLE:why='NOT_RETRYABLE_USE_REPARSE_OR_INPUT_REVIEW'
            else:
                attempts=list_attempts(config,t)
                if st.get('attempt_no')!=len(attempts):why='ATTEMPT_HISTORY_MISMATCH'
                elif len(attempts)>=config.get('execution.max_attempts',3):why='MAX_ATTEMPTS'
                else:
                    audit_journal(config,t);verify_task_inputs(config,t)
                    if attempts:
                        require_integrity(attempts[-1]);obs=attempt_observation(attempts[-1],st)
                        if obs['state']!=state:raise StateError('Latest attempt and task state disagree')
                    chosen.append({'task_id':t.task_id,'status_sha256':canonical_sha256(st),
                        'state':state,'attempt_no':st['attempt_no'],'input_contract_sha256':t.contract_sha256,
                        'attempt_snapshot_sha256':canonical_sha256(snapshot(attempts[-1])) if attempts else None})
            if why:excluded.append({'task_id':t.task_id,'state':state,'reason':why})
        if len(chosen)>max_selected:raise ValueError('Retry target exceeds max_selected; narrow the selection')
        return _plan('retry',config,{'filters':filters,'reason':reason,'selected':chosen,'excluded':excluded})
    if not apply:
        p=prepare();return {**p,'retry_count':len(p['identity']['selected']),'excluded_count':len(p['identity']['excluded'])}
    with locks(config,relevant):
        current=prepare()
        if current['plan_id']!=saved['plan_id']:raise StateError('Retry selection/state changed since preview')
        chosen=current['identity']['selected']
        if not chosen:raise ValueError('No retryable task selected')
        op=within(config.root,'operations/'+saved['plan_id'])
        if op.exists():raise StateError('Plan already applied or incomplete operation needs review')
        op.mkdir(parents=True);atomic_write_json(op/'intent.json',saved)
        completed=[]
        try:
            for row in chosen:
                t=known[row['task_id']]
                st=write_status(config,t,state='PENDING',message='retry approved: '+reason,pid=None,
                    retry_plan_id=saved['plan_id'],retry_config_sha256=config.config_sha256)
                receipt={'task_id':t.task_id,'new_status_sha256':canonical_sha256(st),'revision':st['revision']}
                atomic_write_json(op/(t.task_id+'.json'),receipt);completed.append(receipt)
        except Exception as exc:
            atomic_write_json(op/'PARTIAL.json',{'completed':completed,'error':str(exc)});raise
        result={'status':'APPLIED','plan_id':saved['plan_id'],'retry_count':len(completed),
                'task_ids':[r['task_id'] for r in completed],'receipt_directory':str(op),'native_executed':False}
        atomic_write_json(op/'receipt.json',result);return result


def set_drain(config,enabled,reason='',*,scope='campaign',run_id=None,apply=False,yes=False,plan_file=None):
    _approved(apply,yes);marker=within(config.root,'control/DRAIN')
    if apply:
        saved=_read_plan(plan_file,'drain' if enabled else 'resume',config)
        ident=saved['identity'];scope=ident['scope'];reason=ident['reason'];run_id=ident['run_id']
    if scope not in {'campaign','job','run'}:raise ValueError('scope must be campaign/job/run')
    if enabled and not reason.strip():raise ValueError('Drain reason is required')
    if scope=='job' and not os.environ.get('PBS_JOBID'):raise ValueError('Job-scoped drain requires PBS_JOBID')
    if scope=='run' and not run_id:raise ValueError('Run-scoped drain requires --run-id')
    prior=optional_json(marker)
    p=_plan('drain' if enabled else 'resume',config,{'prior':prior,'scope':scope,'reason':reason,
            'run_id':run_id,'pbs_job_id':os.environ.get('PBS_JOBID','')})
    if not apply:return p
    # Existing campaign.toml inode as a cooperative control-operation lock.
    import fcntl
    # NFS exclusive locks require write-capable descriptors; never write/truncate this file.
    fd=os.open(config.path,os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if p['plan_id']!=saved['plan_id'] or optional_json(marker)!=prior:raise StateError('Drain marker changed after preview')
        op=within(config.root,'operations/'+saved['plan_id'])
        if op.exists():raise StateError('Drain plan already consumed')
        op.mkdir(parents=True);atomic_write_json(op/'intent.json',saved)
        if enabled:
            atomic_write_json(marker,{'schema':'rcm-drain-p06-v1','campaign_root':str(config.root),
                'scope':scope,'run_id':run_id,'pbs_job_id':os.environ.get('PBS_JOBID',''),
                'reason':reason,'creator':host_identity(),'created_utc':utc_now(),
                'expiry':'manual resume for campaign; otherwise matching job/run only'})
        elif marker.exists():
            # Preserve the removed marker as evidence instead of deleting its contents.
            os.rename(marker,op/'removed_marker.json')
        result={'status':'APPLIED','drain':enabled,'plan_id':p['plan_id'],'marker':str(marker)}
        atomic_write_json(op/'receipt.json',result);return result
    finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def inspect_claims(config):
    rows=[]
    for t in iter_tasks(config.root):
        snap=claim_snapshot(config,t)
        if snap:rows.append({'task_id':t.task_id,'claim':snap,'liveness':owner_liveness(snap),
            'state':load_status(config.root,t.task_id)['state']})
    return {'schema':'rcm-claims-p06-v1','status':'OBSERVED','read_only':True,'claims':rows,
            'stale_policy':'Never release based on mtime/clock age; remote owners require scheduler review'}


def release_stale_claim(config,task_id,*,reason='',apply=False,yes=False,plan_file=None,confirm_owner_dead=False):
    _approved(apply,yes);t=load_task(config.root,task_id)
    def prepare():
        snap=claim_snapshot(config,t)
        if snap is None:raise StateError('No active claim')
        return _plan('claim-release',config,{'task_id':task_id,'claim':snap,
             'status_sha256':canonical_sha256(load_status(config.root,task_id)),'reason':reason})
    if not apply:return prepare()
    saved=_read_plan(plan_file,'claim-release',config);reason=saved['identity']['reason']
    if not confirm_owner_dead or not reason.strip():raise PermissionError('Release needs --confirm-owner-dead and a reviewed reason')
    with locks(config,[t]):
        p=prepare()
        if p['plan_id']!=saved['plan_id']:raise StateError('Claim/status changed after preview')
        snap=p['identity']['claim'];live=owner_liveness(snap)
        if live not in {'LOCAL_OWNER_NOT_ALIVE','PREVIOUS_BOOT'}:raise StateError('Claim cannot be safely released: '+live)
        beat=snap.get('heartbeat') or {};native=beat.get('native')
        if native and live!='PREVIOUS_BOOT':
            from .supervisor import group_members
            current=proc_identity(native.get('pid'))
            if (current and current['state'] not in {'Z','X'} and current['start_ticks']==native.get('start_ticks')) or group_members(native.get('pgrp')):
                raise StateError('Native process or group is still alive; release refused')
        st=load_status(config.root,t.task_id)
        if st.get('pid') and not native and live!='PREVIOUS_BOOT':raise StateError('Native identity missing; manual evidence recovery required')
        active=claim_path(config,t);history=within(task_directory(config.root,task_id),'claims/history');history.mkdir(exist_ok=True)
        target=within(history,'recovered_'+saved['plan_id'])
        if target.exists():raise StateError('Claim recovery receipt already exists')
        atomic_write_json(active/'manual_release.json',{'plan':saved,'reason':reason,'utc':utc_now()})
        os.rename(active,target)
        # Do NOT seal or relabel a crashed attempt as successful or automatically queue it.
        return {'status':'RELEASED_FOR_REVIEW','claim_history':str(target),'task_status_unchanged':True,
                'unsealed_attempts_require_manual_review':True}


def recover(config,*,task_ids=None):
    rows=[]
    for t in iter_tasks(config.root):
        if task_ids and t.task_id not in task_ids:continue
        st=load_status(config.root,t.task_id);state=st['state']
        if state=='COMPLETE':continue
        advice=('claims inspect; verify owner/native liveness before release' if claim_path(config,t).exists() or state in ACTIVE else
                'reparse preview; do not rerun a successful engine' if state in {'CONTRACT_FAILED','ENGINE_SUCCEEDED_PARSE_INCOMPLETE','PARTIAL_OUTPUT'} else
                'review timeout/resource cause; then retry preview/apply' if state in RETRYABLE else
                'pending: inspect plan/run --dry-run')
        rows.append({'task_id':t.task_id,'state':state,'recommendation':advice})
    return {'schema':'rcm-recover-p06-v1','status':'PREVIEW','read_only':True,'actions':rows,
            'apply_commands':'retry/reparse/claims release each require their own reviewed plan; no automatic native rerun'}


def reparse_tasks(config:CampaignConfig,*,task_ids=None,**kwargs):
    from .reparse import reparse_tasks as service
    return service(config,task_ids=task_ids,**kwargs)
