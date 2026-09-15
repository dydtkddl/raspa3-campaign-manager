"""Bounded static preview. Never calls doctor, a native engine, or a scheduler."""
from __future__ import annotations
import json
from .config import ConfigError
from .discovery import records_for_gas, discover_cifs, read_descriptor_table
from .inventory import load_inventory
from .input_identity import snapshot_inputs
from .tasks import iter_conditions, make_task
from .ordering import order_policy


def preview_inputs(config, *, limit=5):
    if type(limit) is not int or not 1<=limit<=10000:raise ConfigError('preview limit must be 1..10000')
    inventory=load_inventory(config);conditions=list(iter_conditions(config))
    allowed={g:{r.mof_id for r in records_for_gas(config,inventory,g)} for g in sorted({c['gas'] for c in conditions})}
    expected=sum(sum(r.mof_id in allowed[c['gas']] for c in conditions) for r in inventory)
    rows=[]
    for record in inventory:
        for condition in conditions:
            if record.mof_id not in allowed[condition['gas']]:continue
            task=make_task(config,record,condition)
            contract,payload,paths=snapshot_inputs(config,record,condition)
            rows.append({'task_id':task.task_id,'mof_id':record.mof_id,**condition,
                         'simulation_json':json.loads(payload['simulation.json']),
                         'input_manifest':contract['staged_files'],'source_paths':paths,
                         'raspa_profile':contract.get('raspa_profile'),
                         'native_test':'NOT_RUN'})
            if len(rows)>=limit:break
        if len(rows)>=limit:break
    return {'schema':'rcm-input-preview-p04-v1','status':'PASS','scope':'STATIC_INPUT_PREVIEW_ONLY',
            'total_task_count':expected,'previewed_task_count':len(rows),
            'all_task_inputs_checked':len(rows)==expected,'selected_mof_count':len(inventory),
            'gas_populations':{g:len(ids) for g,ids in allowed.items()},'order_policy':order_policy(config),
            'rows':rows,'engine_executed':False,'writes':False}


def preview_catalog(config, *, limit=20):
    if type(limit) is not int or not 1<=limit<=10000:raise ConfigError('inventory preview limit must be 1..10000')
    records=load_inventory(config)
    conditions=list(iter_conditions(config));gases=sorted({c['gas'] for c in conditions})
    metadata=discover_cifs(config)[1] if config.get('inputs.cif_dir') is not None else read_descriptor_table(config)[1]
    return {'schema':'rcm-catalog-preview-p04-v1','status':'PASS','metadata':metadata,'total_selected':len(records),
            'returned':min(limit,len(records)),'gas_populations':{g:len(records_for_gas(config,records,g)) for g in gases},
            'rows':[{'mof_id':r.mof_id,'cif_path':r.cif_path,**r.descriptors} for r in records[:limit]],
            'pld_calculated':False,'writes':False}
