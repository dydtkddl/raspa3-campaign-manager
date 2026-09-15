"""P06 validated execution controls. No scientific input changes."""
from __future__ import annotations
import asyncio
import math
import os
from pathlib import Path
import signal
import socket
import threading
from .config import ConfigError
from .evidence import optional_json, new_id
from .models import utc_now
from .paths import within


def positive_number(value,key,*,zero=True):
    if type(value) not in (int,float) or not math.isfinite(value) or value < 0 or (not zero and value==0):
        raise ConfigError(key+': finite '+('nonnegative' if zero else 'positive')+' number required')
    return float(value)


def execution_options(config,workers=None,limit=None,allocation=None):
    for key,default in [('workers',1),('max_attempts',3),('threads_per_task',1)]:
        v=config.get('execution.'+key,default)
        if type(v) is not int or not 1<=v<=1000000:raise ConfigError('execution.'+key+': positive integer required')
    if config.get('execution.threads_per_task',1)!=1:raise ConfigError('Only one thread per native task is supported')
    if config.get('execution.auto_retry',False) is not False:raise ConfigError('Automatic retry is disabled; use retry preview/apply')
    n=config.get('execution.workers',1) if workers is None else workers
    if type(n) is not int or n<1:raise ConfigError('workers must be a positive integer')
    available=len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else (os.cpu_count() or 1)
    if config.get('pbs.enabled',False):available=min(available,int(config.get('pbs.ncpus',1)))
    if os.environ.get('RCM_ALLOCATION_NCPUS'):
        v=os.environ['RCM_ALLOCATION_NCPUS']
        if not v.isdigit() or int(v)<1:raise ConfigError('Invalid RCM_ALLOCATION_NCPUS')
        available=min(available,int(v))
    cap=config.get('execution.max_workers',available)
    if type(cap) is not int or cap<1:raise ConfigError('execution.max_workers must be positive integer')
    if n>min(cap,available):raise ConfigError(f'workers={n} exceeds CPU/allocation limit={min(cap,available)}')
    if limit is not None and (type(limit) is not int or limit<1):raise ConfigError('limit must be a positive integer')
    defaults={'timeout_seconds':0,'kill_grace_seconds':30,'heartbeat_seconds':20,
        'collector_interval_seconds':20,'drain_safety_seconds':900,'min_free_bytes':0,'min_free_inodes':0}
    opt={k:positive_number(config.get('execution.'+k,d),'execution.'+k,
                         zero=k not in {'heartbeat_seconds','collector_interval_seconds'}) for k,d in defaults.items()}
    if allocation is not None:positive_number(allocation,'allocation_seconds',zero=False)
    from .reparse import _safe_globs
    opt['output_globs']=_safe_globs(config.get('execution.output_globs',['output/output_*.txt','output_*.txt']))
    opt['json_globs']=_safe_globs(config.get('execution.json_output_globs',['output/output_*.json','output_*.json']))
    return {**opt,'workers':n,'available_cpus':available,'max_attempts':config.get('execution.max_attempts',3)}


class RunControl:
    def __init__(self,config,run_id=None):
        self.run_id=run_id or new_id('run');self.config=config
        self.drain=False;self.cancel=asyncio.Event();self.signals=[];self.saved={}
    def on_signal(self,sig):
        self.signals.append({'signal':signal.Signals(sig).name,'utc':utc_now()})
        self.drain=True
        if sig!=signal.SIGUSR1:self.cancel.set()
    def install(self):
        if threading.current_thread() is not threading.main_thread():
            raise ConfigError('Campaign runner must run on the main thread for reliable signals')
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGUSR1):
            self.saved[sig]=signal.getsignal(sig);loop.add_signal_handler(sig,self.on_signal,sig)
    def restore(self):
        loop=asyncio.get_running_loop()
        for sig,h in self.saved.items():loop.remove_signal_handler(sig);signal.signal(sig,h)
        self.saved.clear()
    def drain_reason(self):
        if self.drain:return 'SIGNAL_DRAIN'
        p=within(self.config.root,'control/DRAIN')
        marker=optional_json(p)
        if marker is None:return None
        if not isinstance(marker,dict) or marker.get('schema')!='rcm-drain-p06-v1':return 'LEGACY_DRAIN_REVIEW_REQUIRED'
        if marker.get('campaign_root')!=str(self.config.root):raise ValueError('Drain marker belongs to a different campaign')
        scope=marker.get('scope')
        if scope=='campaign':return 'CAMPAIGN_DRAIN'
        if scope=='job':return 'JOB_DRAIN' if marker.get('pbs_job_id')==os.environ.get('PBS_JOBID','') else None
        if scope=='run':return 'RUN_DRAIN' if marker.get('run_id')==self.run_id else None
        raise ValueError('Unknown drain marker scope')
