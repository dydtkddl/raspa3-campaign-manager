"""P06 read-only preflight. Never invoke a RASPA version probe without inputs."""
from __future__ import annotations
import os
import shutil
import sys
from .config import CampaignConfig
from .evidence import digest_file
from .inventory import validate_inventory
from .models import utc_now
from .execution_control import execution_options

def doctor(config:CampaignConfig):
    checks=[]
    def add(name,state,detail):checks.append({'name':name,'status':state,'passed':state=='PASS','detail':detail})
    add('python_version','PASS' if sys.version_info>=(3,11) else 'FAIL',sys.version)
    try:add('execution_options','PASS',execution_options(config))
    except Exception as exc:add('execution_options','FAIL',str(exc))
    try:add('inventory','PASS',validate_inventory(config))
    except Exception as exc:add('inventory','FAIL',str(exc))
    info={'path':str(config.engine_binary),'resolved_path':str(config.engine_binary.resolve()),'version_probe':'NOT_RUN_BY_DESIGN'}
    try:
        e=config.engine_binary
        if not e.is_absolute() or not e.is_file() or not os.access(e,os.X_OK):raise ValueError('Absolute executable unavailable')
        # Engine wrapper symlinks are resolved deliberately; managed campaign evidence never is.
        info['sha256']=digest_file(e.resolve())['sha256'];expected=config.get('engine.expected_sha256','')
        add('engine_hash','PASS' if expected==info['sha256'] else 'FAIL' if expected else 'WARN',info)
    except Exception as exc:add('engine_hash','FAIL',str(exc))
    disk=shutil.disk_usage(config.root)
    add('disk_space','PASS' if disk.free>=config.get('execution.min_free_bytes',0) else 'FAIL',{'free_bytes':disk.free})
    add('native_version_probe','NOT_RUN',{'version_args':config.get('engine.version_args',[]),'reason':'Not a calculation or version-execution command'})
    add('production_authorized','PASS' if config.get('campaign.production_authorized',False) is True else 'WARN',
        'This boolean is authorization, not proof of scientific qualification')
    failures=sum(r['status']=='FAIL' for r in checks);warnings=sum(r['status']=='WARN' for r in checks)
    return {'schema':'rcm-doctor-p06-v1','campaign':config.name,'created_utc':utc_now(),
        'status':'FAIL' if failures else 'WARN' if warnings else 'PASS','error_count':failures,'checks':checks,
        'engine':info,'read_only':True,'native_engine_executed':False,
        'not_tested':['native engine compatibility','scheduler submission','filesystem concurrent-claim semantics']}
