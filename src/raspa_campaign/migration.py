"""P06 copy-only import. Default preview; no silent migration or active install changes."""
from __future__ import annotations
import csv
import os
from pathlib import Path
from .atomic import atomic_write_json
from .evidence import snapshot,digest_file,new_id
from .hashing import canonical_sha256
from .initializer import init_campaign
from .paths import no_symlinks,tree_files,within,read_json_file

LEGACY_CANDIDATES={'07result.csv','98_complete.txt','99_progress.log','04cif_list.txt'}

def _plan(source,dest,kind,copy_sources):
    source=no_symlinks(source);dest=no_symlinks(dest)
    if not source.is_dir():raise FileNotFoundError(source)
    if source==dest or dest.is_relative_to(source) or source.is_relative_to(dest):raise ValueError('Import source/destination overlap')
    if dest.exists():raise FileExistsError('Use a new destination for an import')
    files=tree_files(source)
    if kind=='legacy':files=[p for p in files if p.suffix.lower()=='.cif' or p.name in LEGACY_CANDIDATES]
    else:files=[p for p in files if p.relative_to(source).parts[0] in {'campaigns','04_EVIDENCE','07_RELEASES'}]
    if not files:raise ValueError('No selected legacy/data files')
    if kind=='legacy':
        ids=[p.stem for p in files if p.suffix.lower()=='.cif']
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate CIF stem; resolve MOF IDs before import')
    rows=[{'relative_path':p.relative_to(source).as_posix(),**digest_file(p)} for p in files]
    ident={'source':str(source),'destination':str(dest),'kind':kind,'copy_sources':copy_sources,'files':rows}
    return {'schema':'rcm-migration-plan-p06-v1','status':'PREVIEW','identity':ident,'plan_id':'plan_'+canonical_sha256(ident),
            'source_modified':False,'activation_performed':False}

def _apply(source,destination,kind,copy_sources,apply,yes,plan_file):
    p=_plan(source,destination,kind,copy_sources)
    if not apply:return p
    if not yes or plan_file is None:raise PermissionError('Migration needs --plan-file --apply --yes')
    saved=read_json_file(Path(plan_file))
    if saved!=p:raise ValueError('Migration source/selection changed since preview')
    source=Path(p['identity']['source']);dest=Path(p['identity']['destination'])
    temp=dest.with_name('.'+dest.name+'.'+new_id('import'));temp.mkdir(parents=True)
    if kind=='legacy':init_campaign(temp,preset='single_h2_pilot',force=False)
    inventory=[]
    for row in p['identity']['files']:
        src=within(source,row['relative_path'])
        if digest_file(src)!={k:row[k] for k in ['sha256','size_bytes']}:raise ValueError('Source changed during import')
        target_rel=('legacy_import/'+row['relative_path']) if kind=='legacy' else row['relative_path']
        dst=within(temp,target_rel)
        if copy_sources or src.suffix.lower()!='.cif':
            from .atomic import atomic_write_bytes
            dst.parent.mkdir(parents=True,exist_ok=True);atomic_write_bytes(dst,src.read_bytes())
            if digest_file(dst)!=digest_file(src):raise ValueError('Import copy hash mismatch')
        if kind=='legacy' and src.suffix.lower()=='.cif':inventory.append({'mof_id':src.stem,
            'cif_path':str(dest/target_rel) if copy_sources else str(src),'legacy_source':str(src)})
    if inventory:
        with (temp/'inventory/inventory.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(inventory[0]));w.writeheader();w.writerows(inventory)
    if _plan(source,dest,kind,copy_sources)!=p:raise ValueError('Source changed before import publication')
    receipt={**p,'status':'IMPORTED_FOR_REVIEW','destination_manifest':snapshot(temp),
             'production_authorized':False,'boundary':'Imported records do not create COMPLETE task states'}
    atomic_write_json(temp/'MIGRATION_RECEIPT.json',receipt)
    if dest.exists():raise FileExistsError('Destination appeared while importing')
    os.rename(temp,dest);return receipt

def migrate_legacy(legacy_root,destination,*,copy_sources=True,apply=False,yes=False,plan_file=None):
    return _apply(legacy_root,destination,'legacy',copy_sources,apply,yes,plan_file)

def upgrade_install(old_install,new_install,*,apply=False,yes=False,plan_file=None):
    return _apply(old_install,new_install,'install-data',True,apply,yes,plan_file)
