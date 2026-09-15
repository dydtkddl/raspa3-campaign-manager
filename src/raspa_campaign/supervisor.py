"""P06 bounded native execution. Scientific inputs/parsing remain P01/P03/P04/P05."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import sys
import time
from typing import Any
from .atomic import atomic_write_json
from .collector import IncrementalCollector
from .config import CampaignConfig
from .contracts import evaluate_contract
from .evidence import finish_attempt,make_policy,new_id,optional_json
from .hashing import sha256_file,canonical_sha256
from .models import Task,TaskState,utc_now
from .parser import parse_attempt
from .scheduler import load_planned_tasks
from .tasks import load_status,load_task,assert_same_task
from .input_identity import verify_task_inputs
from .paths import attempt_directory,task_directory,within
from .templates import render_text,stage_task,task_environment,verify_staged_inputs
from .execution_state import (locks,BusyError,StateError,eligibility,create_claim,
    release_claim,heartbeat,proc_identity,write_status)
from .execution_control import RunControl,execution_options

class SupervisorError(RuntimeError):pass

# Retained names make old integration tests and inspection tools applicable.
def _write_status(config,task,**updates):return write_status(config,task,**updates)
def _status_path(config,task):return within(task_directory(config.root,task.task_id),'status.json')
def _eligible(config,task,*,retry_failed=False):
    if retry_failed:raise ValueError('--retry-failed bypass removed; use retry --plan-file ... --apply --yes')
    return eligibility(config,task)=='ELIGIBLE'


def _next_attempt(config,task):
    from .evidence import list_attempts,attempt_number
    current=list_attempts(config,task);numbers=[attempt_number(p) for p in current]
    st=load_status(config.root,task.task_id)
    if numbers!=list(range(1,len(numbers)+1)) or st['attempt_no']!=len(numbers):
        raise StateError('Attempt directories/status disagree')
    if len(numbers)>=config.get('execution.max_attempts',3):raise StateError('Maximum attempts reached')
    no=len(numbers)+1;p=attempt_directory(config.root,task.task_id,no);p.mkdir()
    for sub in ('raw','derived','live'):(p/sub).mkdir()
    return no,p


def _command(config,task,attempt_no,attempt_dir):
    env=task_environment(config,task,attempt_no,attempt_dir)
    raw=config.get(f'gas_profiles.{task.gas}.engine_command',config.get('engine.command',['${ENGINE}']))
    if isinstance(raw,str):raw=shlex.split(raw)
    if not isinstance(raw,list) or not raw or any(not isinstance(x,str) or not x or '\x00' in x for x in raw):
        raise ValueError('engine.command must be a nonempty argument list')
    command=[render_text(x,env,strict=True) for x in raw]
    if config.get('execution.use_gnu_time',True) and Path('/usr/bin/time').is_file():
        command=['/usr/bin/time','-v','-o',str(attempt_dir/'derived/gnu_time.txt'),'--']+command
    return command,env


def group_members(pgid):
    """Live members of the session we created, not every process named raspa."""
    if type(pgid) is not int or pgid<=0:return []
    rows=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:i=proc_identity(int(p.name))
        except (OSError,StateError):continue
        if i and i['pgrp']==pgid and i['session']==pgid and i['state'] not in {'Z','X'}:rows.append(i)
    return rows


async def _terminate_group(proc,grace):
    pgid=proc.pid;events=[]
    def send(sig):
        if not group_members(pgid):return
        try:os.killpg(pgid,sig)
        except ProcessLookupError:return
        events.append({'signal':signal.Signals(sig).name,'utc':utc_now(),'monotonic':time.monotonic()})
    send(signal.SIGTERM)
    stop=time.monotonic()+grace
    while group_members(pgid) and time.monotonic()<stop:await asyncio.sleep(min(.02,max(0,stop-time.monotonic())))
    if group_members(pgid):send(signal.SIGKILL)
    try:await asyncio.wait_for(proc.wait(),timeout=max(1.,grace))
    except asyncio.TimeoutError:pass
    deadline=time.monotonic()+2
    while group_members(pgid) and time.monotonic()<deadline:await asyncio.sleep(.02)
    survivors=group_members(pgid)
    return {'signals':events,'remaining_live_members':survivors,'clean':not survivors}


def _collect_final(collector,tid):
    if hasattr(collector,'finalize'):collector.finalize(tid)
    else:collector.scan_once()  # P01/P03/P05 in-process test double compatibility
    collector.unregister(tid)


async def _wait_native(proc,config,task,owner,opt,control):
    began=time.monotonic();last_beat=began;sequence=0;reason=None
    wait=asyncio.create_task(proc.wait());cancel=asyncio.create_task(control.cancel.wait())
    cleanup=None
    try:
        while True:
            if control.cancel.is_set():reason='CANCELLED';break
            remaining=opt['timeout_seconds']-(time.monotonic()-began) if opt['timeout_seconds'] else None
            if remaining is not None and remaining<=0:reason='TIMEOUT';break
            interval=min(opt['heartbeat_seconds'],remaining) if remaining is not None else opt['heartbeat_seconds']
            done,_=await asyncio.wait([wait,cancel],timeout=interval,return_when=asyncio.FIRST_COMPLETED)
            if cancel in done:reason='CANCELLED';break
            if wait in done:
                # A wrapper exiting does not imply that grandchildren exited.
                if group_members(proc.pid):reason='LEFTOVER_PROCESS_GROUP'
                break
            sequence+=1
            native=proc_identity(proc.pid) if proc.pid else None
            heartbeat(config,task,owner,native=native,sequence=sequence)
        if reason:
            cleanup=await _terminate_group(proc,opt['kill_grace_seconds'])
            if cleanup is not None and not cleanup['clean']:raise SupervisorError('Native process group cleanup incomplete')
        return proc.returncode if proc.returncode is not None else (await wait),reason,cleanup
    finally:
        cancel.cancel();await asyncio.gather(cancel,return_exceptions=True)
        if not wait.done():wait.cancel()
        await asyncio.gather(wait,return_exceptions=True)
def _json_text_crosscheck(parsed, json_records: list[dict[str, Any]], tolerance: float = 1e-8) -> dict[str, Any]:
    candidates_loading=[]; candidates_cycle=[]
    for doc in json_records:
        for item in doc.get("flat", []):
            key=str(item.get("path", "")).lower(); val=item.get("value")
            if not isinstance(val, (int, float)):
                continue
            if "loading" in key and ("mol" in key or "kg" in key): candidates_loading.append({"path":item.get("path"),"value":float(val)})
            if "cycle" in key and any(x in key for x in ("final","number","production","current")): candidates_cycle.append({"path":item.get("path"),"value":int(val)})
    loading_match=None
    if parsed.loading_primary_mol_kg is not None and candidates_loading:
        loading_match=min(candidates_loading,key=lambda r:abs(r["value"]-parsed.loading_primary_mol_kg))
    cycle_match=None
    if parsed.last_cycle is not None and candidates_cycle:
        cycle_match=min(candidates_cycle,key=lambda r:abs(r["value"]-parsed.last_cycle))
    return {
        "text_loading_mol_kg":parsed.loading_primary_mol_kg,
        "json_loading_candidates":candidates_loading,
        "closest_loading":loading_match,
        "loading_match":None if loading_match is None or parsed.loading_primary_mol_kg is None else abs(loading_match["value"]-parsed.loading_primary_mol_kg)<=max(tolerance,abs(parsed.loading_primary_mol_kg)*1e-8),
        "text_last_cycle":parsed.last_cycle,
        "json_cycle_candidates":candidates_cycle,
        "closest_cycle":cycle_match,
        "cycle_match":None if cycle_match is None or parsed.last_cycle is None else cycle_match["value"]==parsed.last_cycle,
        "status":"PASS" if ((loading_match is None or parsed.loading_primary_mol_kg is None or abs(loading_match["value"]-parsed.loading_primary_mol_kg)<=max(tolerance,abs(parsed.loading_primary_mol_kg)*1e-8)) and (cycle_match is None or parsed.last_cycle is None or cycle_match["value"]==parsed.last_cycle)) else "REVIEW"
    }


async def run_one(config:CampaignConfig,task:Task,collector,*,dry_run=False,control=None):
    # Validate bytes before any claim/attempt/write, including direct API calls.
    try:
        task_dir=task_directory(config.root,task.task_id)
        assert_same_task(task,load_task(config.root,task.task_id));verify_task_inputs(config,task)
    except (ValueError,OSError,TypeError,KeyError) as exc:
        return {'task_id':task.task_id,'state':'FAILED','attempt':None,'error':str(exc),
                'reason':'PREEXEC_INPUT_REJECTED','engine_executed':False}
    opt=execution_options(config,workers=1)
    if dry_run:
        no=int(load_status(config.root,task.task_id)['attempt_no'])+1
        command,_=_command(config,task,no,attempt_directory(config.root,task.task_id,no))
        return {'task_id':task.task_id,'state':'DRY_RUN','attempt':None,'next_attempt':no,
                'eligibility':eligibility(config,task),'command':command,'engine_executed':False}
    owner=None;attempt_dir=None;proc=None;spawn=None;start=None;release=False
    control=control or RunControl(config)
    try:
        with locks(config,[task]):
            reason=eligibility(config,task)
            if reason!='ELIGIBLE':return {'task_id':task.task_id,'state':'SKIPPED_'+reason,'attempt':None,'engine_executed':False}
            verify_task_inputs(config,task)
            if control.drain_reason():return {'task_id':task.task_id,'state':'DRAINED_BEFORE_START','attempt':None}
            fs=os.statvfs(config.root)
            if fs.f_bavail*fs.f_frsize<opt['min_free_bytes'] or fs.f_favail<opt['min_free_inodes']:
                control.drain=True
                return {'task_id':task.task_id,'state':'DRAINED_STORAGE','attempt':None}
            owner=create_claim(config,task,control.run_id)
            try:
                attempt_no,attempt_dir=_next_attempt(config,task)
                _write_status(config,task,state='CLAIMED',attempt_no=attempt_no,pid=None,exit_code=None,
                    parse_status='NOT_RUN',contract_status='NOT_RUN',wall_seconds=None,
                    hostname=socket.gethostname(),pbs_job_id=os.environ.get('PBS_JOBID',''),
                    message='staging',claim_id=owner['claim_id'],run_id=control.run_id)
                staged=stage_task(config,task,attempt_no,attempt_dir)
                atomic_write_json(attempt_dir/'derived/staging_manifest.json',staged)
                command,env=_command(config,task,attempt_no,attempt_dir)
                from . import __version__
                from importlib.resources import files
                from .hashing import sha256_bytes
                package_hash=canonical_sha256({p.name:sha256_bytes(p.read_bytes())
                    for p in files('raspa_campaign').iterdir() if p.name.endswith('.py') and p.is_file()})
                launch={'task_id':task.task_id,'attempt_no':attempt_no,'command':command,
                    'cwd':str(attempt_dir/'work'),'engine_binary':str(config.engine_binary),
                    'engine_binary_sha256':sha256_file(config.engine_binary) if config.engine_binary.is_file() else None,
                    'engine_resolved':str(config.engine_binary.resolve()),'manager_version':__version__,
                    'manager_source_sha256':package_hash,'python':sys.version,'python_executable':sys.executable,
                    'run_id':control.run_id,'claim':owner,'evidence_policy':make_policy(config,task),
                    'operating_config_sha256':config.config_sha256,'execution':opt,
                    'environment':{k:env.get(k,'') for k in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS','PBS_JOBID')},
                    'started_utc':utc_now()}
                atomic_write_json(attempt_dir/'derived/launch_contract.json',launch)
                stdout=attempt_dir/'raw/stdout.log';stderr=attempt_dir/'raw/stderr.log'
                collector.register(task.task_id,attempt_dir,opt['output_globs'])
                start=time.monotonic();reason=None;cleanup=None
                with stdout.open('wb') as out,stderr.open('wb') as err:
                    verify_staged_inputs(task,attempt_dir,staged,before_launch=True)
                    if control.cancel.is_set():raise asyncio.CancelledError
                    spawn=asyncio.create_task(asyncio.create_subprocess_exec(*command,cwd=attempt_dir/'work',
                        env=env,stdin=asyncio.subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True))
                    proc=await asyncio.shield(spawn)
                    _write_status(config,task,state='RUNNING',pid=proc.pid,message='engine running')
                    heartbeat(config,task,owner,native=proc_identity(proc.pid) if proc.pid else None)
                    code,reason,cleanup=await _wait_native(proc,config,task,owner,opt,control)
                wall=time.monotonic()-start
                _collect_final(collector,task.task_id)
                post=verify_staged_inputs(task,attempt_dir,staged)
                atomic_write_json(attempt_dir/'derived/input_postcheck.json',post)
                expected=launch['evidence_policy']['expected_final_cycle']
                parsed,docs=parse_attempt(attempt_dir,opt['output_globs'],expected_final_cycle=expected,json_globs=opt['json_globs'])
                contract=evaluate_contract(config,task,parsed,exit_code=code,expected_final_cycle=expected)
                atomic_write_json(attempt_dir/'derived/parsed_result.json',parsed.to_dict())
                atomic_write_json(attempt_dir/'derived/json_output_crosscheck.json',{'documents':docs,'crosscheck':_json_text_crosscheck(parsed,docs)})
                atomic_write_json(attempt_dir/'derived/contract_report.json',contract)
                atomic_write_json(attempt_dir/'derived/resource_usage.json',{'exit_code':code,'external_wall_seconds':wall,
                    'hostname':socket.gethostname(),'pbs_job_id':os.environ.get('PBS_JOBID',''),
                    'started_utc':launch['started_utc'],'ended_utc':utc_now(),'interrupted':reason,'process_cleanup':cleanup})
                state=(reason if reason in {'TIMEOUT','CANCELLED'} else 'FAILED' if code!=0 or reason else
                    'ENGINE_SUCCEEDED_PARSE_INCOMPLETE' if parsed.parse_status=='NO_RESULT_FILE' else
                    'CONTRACT_FAILED' if contract['status']!='PASS' else 'COMPLETE')
                finish_attempt(config,task,attempt_dir,state=state,exit_code=code,wall_seconds=wall)
                _write_status(config,task,state=state,pid=None,exit_code=code,wall_seconds=wall,
                    parse_status=parsed.parse_status,contract_status=contract['status'],
                    message='completed' if state=='COMPLETE' else ('requires review: '+str(reason or state)))
                release=True
                return {'task_id':task.task_id,'state':state,'attempt':attempt_no,'wall_seconds':wall,
                    'loading_mol_kg':parsed.loading_primary_mol_kg,'exit_code':code,'engine_executed':True,
                    'interrupted':reason,'claim_id':owner['claim_id']}
            except (Exception,asyncio.CancelledError) as exc:
                cancelled=isinstance(exc,asyncio.CancelledError)
                # Spawn may have completed immediately before cancellation was delivered.
                if proc is None and spawn is not None:
                    try:proc=await asyncio.shield(spawn)
                    except Exception:pass
                cleanup=None
                if proc is not None:cleanup=await _terminate_group(proc,opt['kill_grace_seconds'])
                clean=cleanup is None or cleanup.get('clean',False)
                state='CANCELLED' if cancelled else 'FAILED';error=f'{type(exc).__name__}: {exc}'
                record={'type':type(exc).__name__,'message':str(exc),'utc':utc_now(),'process_cleanup':cleanup}
                if attempt_dir is not None:
                    if (attempt_dir/'SEALED.json').exists():
                        d=within(task_dir,'postseal_errors');d.mkdir(exist_ok=True)
                        atomic_write_json(d/(new_id('error')+'.json'),record)
                    else:
                        try:_collect_final(collector,task.task_id)
                        except Exception as collect_exc:record['collector_error']=str(collect_exc)
                        collector.unregister(task.task_id)
                        atomic_write_json(attempt_dir/'derived/orchestrator_error.json',record)
                        code=proc.returncode if proc is not None else None
                        wall=time.monotonic()-start if start is not None else 0.
                        atomic_write_json(attempt_dir/'derived/resource_usage.json',{'exit_code':code,
                            'external_wall_seconds':wall,'interrupted':state,'process_cleanup':cleanup,'ended_utc':utc_now()})
                        if clean:finish_attempt(config,task,attempt_dir,state=state,exit_code=code,wall_seconds=wall)
                    _write_status(config,task,state=state,pid=None if clean else (proc.pid if proc else None),
                        message='orchestrator error: '+error)
                release=clean and attempt_dir is not None and (attempt_dir/'SEALED.json').exists()
                return {'task_id':task.task_id,'state':state,'attempt':attempt_no if attempt_dir else None,
                    'error':error,'engine_executed':proc is not None,'cleanup':cleanup}
            finally:
                if collector is not None:collector.unregister(task.task_id)
                if owner is not None and release:release_claim(config,task,owner,'attempt terminal and native group stopped')
    except BusyError:
        return {'task_id':task.task_id,'state':'SKIPPED_ALREADY_CLAIMED','attempt':None,'engine_executed':False}
    except (ValueError,OSError,TypeError,KeyError) as exc:
        # Keep unsealed attempts/claims if storage or status publication failed.
        return {'task_id':task.task_id,'state':'FAILED','attempt':None if attempt_dir is None else attempt_no,
                'error':str(exc),'reason':'STATE_OR_STORAGE_REVIEW_REQUIRED','engine_executed':proc is not None}


def summarize(config,selected,eligible,excluded,results,control,workers,chunk_id,dry_run,wall):
    failed=[r for r in results if r['state'] not in {'COMPLETE','DRY_RUN'} and not r['state'].startswith('DRAINED')]
    excluded_fail=[r for r in excluded if r['reason']!='ALREADY_COMPLETE']
    drained=sum(r['state'].startswith('DRAINED') for r in results)
    unstarted=max(0,len(eligible)-len(results))+drained
    complete=sum(r['state']=='COMPLETE' for r in results)
    if dry_run:status='PREVIEW';code=0
    elif failed or excluded_fail or control.cancel.is_set():status='FAIL';code=3
    elif unstarted:status='DRAINED';code=1
    elif not results:status='NO_WORK';code=1
    else:status='PASS';code=0
    return {'schema':'rcm-run-summary-p06-v1','campaign':config.name,'run_id':control.run_id,
        'status':status,'exit_code':code,'workers':workers,'chunk_id':chunk_id,
        'selected_task_count':len(selected),'eligible_task_count':len(eligible),
        'started':sum(bool(r.get('engine_executed')) for r in results),'complete':complete,
        'completed_invocations':len(results),'remaining_unstarted':unstarted,'results':results,
        'excluded':excluded,'unresolved_failure_count':len(failed)+len(excluded_fail),
        'signals':control.signals,'dry_run':dry_run,'wall_seconds':wall,
        'scope':'execution workflow, not convergence or physical model validation'}


async def run_campaign_async(config,*,workers=None,chunk_id=None,limit=None,dry_run=False,retry_failed=False,allocation_seconds=None):
    if retry_failed:raise ValueError('--retry-failed removed: use retry preview, then --apply --yes')
    opt=execution_options(config,workers,limit,allocation_seconds);workers=opt['workers']
    all_tasks=load_planned_tasks(config,chunk_id=chunk_id)
    selected=all_tasks[:limit] if limit is not None else all_tasks
    eligible=[];excluded=[]
    for t in selected:
        reason=eligibility(config,t)
        if reason=='ELIGIBLE':verify_task_inputs(config,t);eligible.append(t)
        else:excluded.append({'task_id':t.task_id,'reason':reason})
    control=RunControl(config);start=time.monotonic();results=[]
    if dry_run:
        for t in eligible:results.append(await run_one(config,t,None,dry_run=True,control=control))
        return summarize(config,selected,eligible,excluded,results,control,workers,chunk_id,True,time.monotonic()-start)
    collector=IncrementalCollector(config.root,interval_seconds=opt['collector_interval_seconds'])
    priority=str(config.get('scheduling.priority_mode','soft')).lower();gp=list(config.get('scheduling.gas_priority',[]))
    batches=([ [t for t in eligible if t.gas==g] for g in gp+sorted({t.gas for t in eligible}-set(gp))]
             if priority in {'strict','strict-staged','strict_staged'} else [eligible])
    workers_live=[];control.install();ct=asyncio.create_task(collector.run())
    try:
        for batch in batches:
            queue=iter(batch);ended=False
            async def worker():
                nonlocal ended
                while not ended:
                    if control.drain_reason():ended=True;return
                    try:t=next(queue)
                    except StopIteration:return
                    if allocation_seconds is not None and allocation_seconds-(time.monotonic()-start)<t.estimated_seconds*1.25+opt['drain_safety_seconds']:
                        control.drain=True;ended=True;return
                    result=await run_one(config,t,collector,control=control);results.append(result)
            workers_live=[asyncio.create_task(worker()) for _ in range(workers)]
            await asyncio.gather(*workers_live)
            if control.drain_reason():break
    except BaseException:
        control.cancel.set();control.drain=True
        for t in workers_live:
            if not t.done():t.cancel()
        await asyncio.gather(*workers_live,return_exceptions=True)
        raise
    finally:
        collector.request_stop()
        try:await ct
        finally:control.restore()
    report=summarize(config,selected,eligible,excluded,results,control,workers,chunk_id,False,time.monotonic()-start)
    p=within(config.root,'reports/'+control.run_id+'.json');atomic_write_json(p,report)
    return {**report,'report_path':str(p)}


def run_campaign(config:CampaignConfig,**kwargs):
    if not kwargs.get('dry_run'):
        if config.get('campaign.production_authorized',False) is not True:raise PermissionError('campaign.production_authorized must be true after input review')
        engine=config.engine_binary
        if not engine.is_absolute() or not engine.is_file() or not os.access(engine,os.X_OK):raise ValueError('Configured absolute engine is not executable')
        expected=config.get('engine.expected_sha256','')
        if expected and sha256_file(engine)!=expected:raise ValueError('Engine SHA-256 mismatch')
    return asyncio.run(run_campaign_async(config,**kwargs))
