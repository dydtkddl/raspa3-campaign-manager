"""Explicit single-framework/single-adsorbate RASPA3 input profiles (P04).

The opt-in layer serializes user choices; it does not supply force-field rows,
charges, molecule geometry or unsupported-native-key substitutions.
"""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path

from .config import CampaignConfig, ConfigError
from .discovery import path_from
from .hashing import sha256_bytes
from .paths import read_regular, safe_name
from .cell_geometry import replication

PROFILE_KEYS={'enabled','force_field_file','molecule_file','global','system','component','force_field','unitcells'}
GLOBAL={
    'NumberOfEquilibrationCycles':'nonnegative_int','WriteBinaryRestartEvery':'positive_int',
    'OptimizeMCMovesEvery':'positive_int','RescaleWangLandauEvery':'positive_int',
    'ThreadingType':('Serial',),'NumberOfThreads':(1,),
}
SYSTEM={
    'ChargeMethod':('None','Ewald'),'UseChargesFrom':('PseudoAtoms','CIF_File','ChargeEquilibration'),
    'CutOffVDW':'positive','CutOffFrameworkVDW':'positive','CutOffMoleculeVDW':'positive','CutOffCoulomb':'positive',
    'HeliumVoidFraction':'fraction','OutputPDBMovie':'bool','SampleMovieEvery':'positive_int',
    'ComputeEnergyHistogram':'bool','SampleEnergyHistogramEvery':'positive_int','WriteEnergyHistogramEvery':'positive_int',
    'ComputeNumberOfMoleculesHistogram':'bool','SampleNumberOfMoleculesHistogramEvery':'positive_int',
    'WriteNumberOfMoleculesHistogramEvery':'positive_int',
}
COMPONENT={k:'nonnegative' for k in ('TranslationProbability','RotationProbability','ReinsertionProbability',
                                   'SwapProbability','SwapConventionalProbability','WidomProbability')}
COMPONENT.update(FugacityCoefficient='positive',CreateNumberOfMolecules='nonnegative_int')
FORCEFIELD={
    'CutOffFrameworkVDW':'positive','CutOffMoleculeVDW':'positive','CutOffCoulomb':'positive',
    'MixingRule':('Lorentz-Berthelot','Jorgensen'), 'TruncationMethod':('truncated','shifted'), 'TailCorrections':'bool',
}


def checked_json(raw: bytes, label: str):
    def pairs(items):
        d={}
        for k,v in items:
            if k in d:raise ConfigError('Duplicate JSON key in '+label+': '+k)
            d[k]=v
        return d
    try:
        obj=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (ValueError,UnicodeError) as exc:
        raise ConfigError('Invalid JSON '+label+': '+str(exc)) from exc
    if not isinstance(obj,dict):raise ConfigError(label+': JSON object required')
    def finite(value):
        if isinstance(value,float) and not math.isfinite(value):
            raise ConfigError('Nonfinite JSON number in '+label)
        if isinstance(value,dict):
            for child in value.values():finite(child)
        elif isinstance(value,list):
            for child in value:finite(child)
    finite(obj)
    return obj


def merged_profile(config: CampaignConfig, gas: str) -> dict:
    base=config.get('raspa',{});specific=config.get(f'gas_profiles.{gas}.raspa',{})
    for label,part in [('raspa',base),(f'gas_profiles.{gas}.raspa',specific)]:
        if not isinstance(part,dict) or set(part)-PROFILE_KEYS:
            raise ConfigError(label+': unknown profile key or non-table')
        if 'enabled' in part and type(part['enabled']) is not bool:
            raise ConfigError(label+'.enabled must be boolean')
    result=copy.deepcopy(base)
    for k,v in specific.items():
        if k in ('global','system','component','force_field','unitcells'):
            if not isinstance(v,dict) or not isinstance(result.get(k,{}),dict):
                raise ConfigError('RASPA option sections must be tables')
            result[k]={**result.get(k,{}),**v}
        else:result[k]=v
    if result and result.get('enabled') is not True:
        if set(result)-{'enabled'}:raise ConfigError('RASPA options were supplied without raspa.enabled=true')
        return {}
    return result


def validate_options(section: str, choices: dict, schema: dict) -> None:
    if not isinstance(choices,dict) or set(choices)-set(schema):
        unknown=sorted(set(choices)-set(schema)) if isinstance(choices,dict) else ['not a table']
        raise ConfigError(f'raspa.{section}: unsupported override keys {unknown}; use the correct native key or an explicitly prepared template')
    for key,value in choices.items():
        rule=schema[key];ok=False
        if isinstance(rule,tuple):
            ok=not isinstance(value,bool) and value in rule
        elif rule=='bool':ok=type(value) is bool
        elif rule.endswith('_int'):
            ok=type(value) is int and (0 if rule=='nonnegative_int' else 1)<=value<=2147483647
        else:
            ok=type(value) in (int,float) and math.isfinite(value) and (value>0 if rule=='positive' else value>=0)
            if rule=='fraction':ok=ok and value<=1
        if not ok:raise ConfigError(f'raspa.{section}.{key}: invalid value {value!r}, required {rule}')


def apply_profile(config: CampaignConfig, record, condition: dict, cycles: dict,
                  payload: dict[str,bytes], sources: list[dict], source_paths: dict[str,str]) -> dict | None:
    profile=merged_profile(config,condition['gas'])
    if not profile:return None
    if str(config.get('engine.expected_version','3.0.29'))!='3.0.29':
        raise ConfigError('P04 typed input helper is scoped to RASPA3 3.0.29; other versions require explicit template review')
    for name,schema in [('global',GLOBAL),('system',SYSTEM),('component',COMPONENT),('force_field',FORCEFIELD)]:
        validate_options(name,profile.get(name,{}),schema)
    sim=checked_json(payload['simulation.json'],'simulation.json')
    if sim.get('SimulationType')!='MonteCarlo':
        raise ConfigError('P04 profile helper supports MonteCarlo only; untouched templates remain available')
    systems=sim.get('Systems');components=sim.get('Components')
    if not isinstance(systems,list) or len(systems)!=1 or not isinstance(components,list) or len(components)!=1:
        raise ConfigError('P04 profile helper requires one system and one component')
    if not isinstance(systems[0],dict) or not isinstance(components[0],dict) or systems[0].get('Type')!='Framework':
        raise ConfigError('P04 profile helper requires a framework system')
    if 'FileName' in systems[0] or 'FileName' in components[0]:
        raise ConfigError('RASPA3 3.0.29 uses Name, not FileName; correct the source template explicitly')
    changes=[]
    def update(target,choices,prefix):
        for key,value in choices.items():
            if target.get(key)!=value or key not in target:
                changes.append({'path':prefix+key,'before':target.get(key),'after':value})
                target[key]=value
    # This opt-in helper binds matrix/cycle fields so a literal template does not
    # accidentally produce many identical BEFNAD or one-pressure tasks.
    update(sim, {'NumberOfCycles':cycles['production'],'NumberOfInitializationCycles':cycles['initialization'],
                 'PrintEvery':cycles['print_every'],'RandomSeed':condition['seed']},'')
    update(systems[0],{'Name':record.mof_id,'ExternalTemperature':condition['temperature_K'],
                       'ExternalPressure':condition['pressure_Pa']},'Systems[0].')
    update(components[0],{'Name':condition['gas']},'Components[0].')
    update(sim,profile.get('global',{}),'')
    update(systems[0],profile.get('system',{}),'Systems[0].')
    update(components[0],profile.get('component',{}),'Components[0].')
    if sim.get('NumberOfThreads',1)!=1 or sim.get('ThreadingType','Serial')!='Serial':
        raise ConfigError('P04 campaign resource model is single-thread per RASPA task')
    if sim.get('RestartFromBinaryFile') is True:
        raise ConfigError('Binary restart selection is not supported by the new-input helper; use a reviewed restart workflow')
    assets=[]
    for config_key,dest,role in [('force_field_file','force_field.json','selected_force_field'),
                                ('molecule_file',condition['gas']+'.json','selected_molecule')]:
        selected=profile.get(config_key)
        if selected is not None:
            path=path_from(config,selected)
            if path.is_dir() and config_key=='force_field_file':path=path/'force_field.json'
            if path.suffix.lower()!='.json':raise ConfigError('Select an existing RASPA3 JSON asset; RASPA2 .def conversion is not implicit')
            raw=read_regular(path,maximum_bytes=64*1024*1024);checked_json(raw,str(path))
            payload[dest]=raw;source_paths[dest]=str(path)
            sources[:]=[r for r in sources if r['path']!=dest]
            sources.append({'role':role,'path':dest,'size_bytes':len(raw),'sha256':sha256_bytes(raw)})
        if dest not in payload:
            raise ConfigError('Missing selected/local native asset: '+dest)
        assets.append({'role':role,'path':dest,'input_sha256':sha256_bytes(payload[dest])})
    # Select the staged local force_field.json, never an untracked installation copy.
    for obj,prefix in [(sim,''),(systems[0],'Systems[0].')]:
        if 'ForceField' in obj:
            if profile.get('force_field_file') is None:
                raise ConfigError('Template selects an external ForceField; explicitly select force_field_file to bind a local copy')
            changes.append({'path':prefix+'ForceField','before':obj['ForceField'],'after':None})
            del obj['ForceField']
    ff=checked_json(payload['force_field.json'],'force_field.json')
    if 'PseudoAtoms' not in ff or 'SelfInteractions' not in ff:
        raise ConfigError('Selected force_field.json must contain explicit PseudoAtoms and SelfInteractions; no parameter synthesis')
    update(ff,profile.get('force_field',{}),'force_field.json.')
    if 'CutOffCoulomb' in systems[0]:
        if ff.get('CutOffCoulomb')!=systems[0]['CutOffCoulomb']:
            raise ConfigError('Fixed Coulomb cutoff must agree with force_field.json in the 3.0.29 profile; set raspa.force_field.CutOffCoulomb explicitly')
    if systems[0].get('UseChargesFrom')=='CIF_File' and b'_atom_site_charge' not in payload[record.mof_id+'.cif'].lower():
        raise ConfigError('CIF_File charge source selected but _atom_site_charge is absent; charges will not be invented')
    if profile.get('force_field'):
        payload['force_field.json']=(json.dumps(ff,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    unit=profile.get('unitcells',{});unit_report=None
    if not isinstance(unit,dict) or set(unit)-{'mode','cutoff_A','values'}:
        raise ConfigError('raspa.unitcells supports mode, cutoff_A, values only')
    mode=unit.get('mode','from_template')
    if mode not in ('from_template','fixed','auto_face_heights'):
        raise ConfigError('unitcells.mode must be from_template, fixed, or auto_face_heights')
    if mode=='from_template' and set(unit)-{'mode'}:
        raise ConfigError('from_template mode does not consume cutoff_A/values')
    if mode=='fixed':
        if set(unit)-{'mode','values'}:raise ConfigError('fixed mode consumes values only')
        values=unit.get('values')
        if not isinstance(values,list) or len(values)!=3 or any(type(n) is not int or not 1<=n<=10000 for n in values):
            raise ConfigError('unitcells.values requires three positive integers')
        unit_report={'mode':mode,'number_of_unit_cells':values}
        update(systems[0],{'NumberOfUnitCells':values},'Systems[0].')
    if mode=='auto_face_heights':
        if 'values' in unit:raise ConfigError('auto_face_heights does not consume fixed values')
        cutoff=unit.get('cutoff_A')
        if systems[0].get('ChargeMethod')=='Ewald' and type(ff.get('CutOffCoulomb')) not in (int,float):
            raise ConfigError('auto_face_heights with Ewald requires explicit fixed CutOffCoulomb in force_field.json')
        if not any(key in obj for obj in (ff,systems[0]) for key in ('CutOffVDW','CutOffFrameworkVDW')):
            raise ConfigError('auto_face_heights requires an explicit framework VDW cutoff; no assumed native default')
        unit_report=replication(payload[record.mof_id+'.cif'],cutoff)
        for obj in (ff,systems[0]):
            for key in ('CutOffVDW','CutOffFrameworkVDW','CutOffMoleculeVDW','CutOffCoulomb'):
                if key in obj and (type(obj[key]) not in (int,float) or not math.isfinite(obj[key]) or obj[key]<=0 or obj[key]>cutoff):
                    raise ConfigError('unitcells.cutoff_A must cover every explicitly specified positive real-space cutoff')
        update(systems[0],{'NumberOfUnitCells':unit_report['number_of_unit_cells']},'Systems[0].')
    values=systems[0].get('NumberOfUnitCells')
    if not isinstance(values,list) or len(values)!=3 or any(type(n) is not int or n<1 for n in values):
        raise ConfigError('Valid NumberOfUnitCells is required; enable explicit auto or fixed mode')
    payload['simulation.json']=(json.dumps(sim,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    # Do not include host-dependent source paths in identity. Exact source hashes
    # and the effective choices are included; absolute paths stay in staging provenance.
    normalized={k:v for k,v in profile.items() if k not in ('force_field_file','molecule_file')}
    return {'schema':'rcm-raspa-profile-p04-v1','native_version':'3.0.29','options':normalized,
            'assets':assets,'unitcells':unit_report,'effective_conditions':condition,
            'changed_json_fields':changes,'no_forcefield_synthesis':True}
