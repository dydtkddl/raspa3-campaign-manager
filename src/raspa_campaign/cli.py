from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .archive import archive_campaign
from .audit import audit_campaign
from .collector import IncrementalCollector
from .config import ConfigError, load_config
from .input_preview import preview_inputs, preview_catalog
from .dashboard import serve_dashboard
from .doctor import doctor
from .exporter import export_campaign
from .files import compress_completed_outputs
from .initializer import init_campaign
from .inventory import validate_inventory
from .migration import migrate_legacy, upgrade_install
from .pbs import render_pbs, submit_pbs
from .qualification import qualification_plan, qualification_report
from .qc import cross_engine_qc, monotonicity_qc
from .recovery import reparse_tasks, retry_tasks, set_drain, recover, inspect_claims, release_stale_claim
from .runtime import fit_runtime_model, historical_wall_seconds
from .scheduler import parse_duration, plan_campaign
from .status import campaign_status, task_detail
from .storage import estimate_storage
from .supervisor import run_campaign
from .tasks import build_tasks, iter_tasks


def emit(value: Any, *, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(value)


def _add_root(p: argparse.ArgumentParser) -> None:
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument("--root",type=Path,help="Campaign root containing campaign.toml")
    group.add_argument("--config",type=Path,help="Path to campaign.toml")


def _cfg(args):
    return load_config(args.config if getattr(args,"config",None) else args.root)


def build_parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="raspa-campaign",description="RASPA3 campaign manager: resumable execution, broad parsing, LPT planning, PBS, QC, and local dashboard")
    p.add_argument("--version",action="version",version=f"%(prog)s {__version__}")
    sub=p.add_subparsers(dest="command",required=True)

    sp=sub.add_parser("init",help="Create a new campaign skeleton")
    sp.add_argument("path",type=Path);sp.add_argument("--preset",default="single_h2_pilot",choices=["single_h2_pilot","ispt_multigas_293K","multigas_qualification_short"]);sp.add_argument("--force",action="store_true")

    sp=sub.add_parser("doctor",help="Check environment, engine, inputs, filesystem, and disk")
    _add_root(sp)
    sp=sub.add_parser("validate",help="Validate campaign config and inventory")
    _add_root(sp)
    sp=sub.add_parser("build",help="Materialize immutable task contracts")
    _add_root(sp);sp.add_argument("--limit",type=int)
    sp=sub.add_parser("plan",help="Estimate runtime and create LPT-balanced chunks")
    _add_root(sp);sp.add_argument("--chunks",type=int);sp.add_argument("--no-refit",action="store_true");sp.add_argument("--preview",action="store_true")
    sp=sub.add_parser("inventory",help="Preview CIF folder/CSV catalog and explicit gas selections; no writes")
    _add_root(sp);sp.add_argument("--limit",type=int,default=20)
    sp=sub.add_parser("preview-inputs",help="Preview resolved native inputs without writing or running RASPA")
    _add_root(sp);sp.add_argument("--limit",type=int,default=5)
    sp=sub.add_parser("estimate-storage",help="Project output storage from completed/pilot output files")
    _add_root(sp);sp.add_argument("--sample",type=Path,action="append",default=[]);sp.add_argument("--safety-factor",type=float,default=1.25)

    sp=sub.add_parser("run",help="Run planned tasks with a bounded RASPA3 process pool")
    _add_root(sp);sp.add_argument("--workers",type=int);sp.add_argument("--chunk",type=int);sp.add_argument("--limit",type=int);sp.add_argument("--dry-run",action="store_true");sp.add_argument("--retry-failed",action="store_true");sp.add_argument("--allocation",help="Remaining/total allocation, e.g. 08:00:00 or 8h");sp.add_argument("--confirm-production",action="store_true",help="Explicitly authorize native engine execution")

    sp.add_argument("--json",action="store_true")

    sp=sub.add_parser("collect",help="Show task-local collection snapshots; runner owns live writes")
    _add_root(sp)
    sp=sub.add_parser("status",help="Show campaign progress")
    _add_root(sp);sp.add_argument("--state",action="append");sp.add_argument("--gas");sp.add_argument("--mof");sp.add_argument("--show-tasks",action="store_true");sp.add_argument("--json",action="store_true")
    sp=sub.add_parser("task",help="Show one task and all attempt evidence")
    _add_root(sp);sp.add_argument("task_id");sp.add_argument("--json",action="store_true")
    sp=sub.add_parser("retry",help="Preview retries; apply requires a reviewed plan and --yes")
    _add_root(sp);sp.add_argument("--task",action="append",dest="tasks");sp.add_argument("--state",action="append");sp.add_argument("--reason",default="manual review")
    sp.add_argument("--gas",action="append");sp.add_argument("--chunk",type=int)
    sp.add_argument("--max-selected",type=int,default=1000);sp.add_argument("--plan-file",type=Path)
    sp.add_argument("--apply",action="store_true");sp.add_argument("--yes",action="store_true");sp.add_argument("--json",action="store_true")
    sp=sub.add_parser("recover",help="Read-only recovery recommendations; never silently retries")
    _add_root(sp);sp.add_argument("--task",action="append",dest="tasks");sp.add_argument("--json",action="store_true")
    cs=sub.add_parser("claims",help="Inspect claims or approve a proven-dead local owner release").add_subparsers(dest="claims_command",required=True)
    sp=cs.add_parser("inspect");_add_root(sp);sp.add_argument("--json",action="store_true")
    sp=cs.add_parser("release");_add_root(sp);sp.add_argument("task_id");sp.add_argument("--reason",default="")
    sp.add_argument("--plan-file",type=Path);sp.add_argument("--apply",action="store_true");sp.add_argument("--yes",action="store_true")
    sp.add_argument("--confirm-owner-dead",action="store_true");sp.add_argument("--json",action="store_true")
    sp=sub.add_parser("reparse",help="Reparse native outputs without running RASPA3")
    _add_root(sp);sp.add_argument("--task",action="append",dest="tasks")
    sp.add_argument("--all-attempts",action="store_true");sp.add_argument("--apply",action="store_true")
    sp.add_argument("--yes",action="store_true");sp.add_argument("--plan-file",type=Path)
    sp.add_argument("--no-select",action="store_true");sp.add_argument("--json",action="store_true")
    sp=sub.add_parser("export",help="Create verified immutable CSV and optional Parquet tables")
    _add_root(sp);sp.add_argument("--all-attempts",action="store_true")
    formats=sp.add_mutually_exclusive_group();formats.add_argument("--parquet",action="store_true");formats.add_argument("--no-parquet",action="store_true")
    sp.add_argument("--require-parquet",action="store_true");sp.add_argument("--task",action="append",dest="tasks")
    sp.add_argument("--state",action="append");sp.add_argument("--gas",action="append")
    sp.add_argument("--metadata-csv",type=Path);sp.add_argument("--metadata-id-column",default="filename")
    sp.add_argument("--json",action="store_true")
    sp=sub.add_parser("audit",help="Read-only rehash of original attempts and derived revisions")
    _add_root(sp);sp.add_argument("--require-all-complete",action="store_true")
    sp.add_argument("--mode",choices=["safe","fast"],default="safe")
    sp.add_argument("--scope",choices=["all-attempts","latest","structure"],default="all-attempts")
    sp.add_argument("--json",action="store_true")
    sp=sub.add_parser("compress-logs",help="Create verified gzip copies of COMPLETE native text outputs")
    _add_root(sp);sp.add_argument("--delete-raw",action="store_true");sp.add_argument("--yes",action="store_true")

    for command in ("drain","resume"):
        sp=sub.add_parser(command,help="Preview/apply scoped drain marker change")
        _add_root(sp);sp.add_argument("--reason",default="manual "+command)
        sp.add_argument("--scope",choices=["campaign","job","run"],default="campaign");sp.add_argument("--run-id")
        sp.add_argument("--plan-file",type=Path);sp.add_argument("--apply",action="store_true");sp.add_argument("--yes",action="store_true");sp.add_argument("--json",action="store_true")

    q=sub.add_parser("qualify",help="Build/report a small gas-condition qualification lane")
    qs=q.add_subparsers(dest="qualify_command",required=True)
    sp=qs.add_parser("plan");_add_root(sp);sp.add_argument("--per-condition",type=int,default=6)
    sp=qs.add_parser("report");_add_root(sp)

    qc=sub.add_parser("qc",help="Scientific screening QC; does not declare parity automatically")
    qcs=qc.add_subparsers(dest="qc_command",required=True)
    sp=qcs.add_parser("monotonicity");_add_root(sp);sp.add_argument("--input",type=Path);sp.add_argument("--abs-tolerance",type=float,default=0.0);sp.add_argument("--sigma",type=float,default=2.0)
    sp=qcs.add_parser("cross-engine");_add_root(sp);sp.add_argument("--legacy",type=Path,required=True);sp.add_argument("--new",type=Path,required=True);sp.add_argument("--relative-tolerance",type=float,default=0.10);sp.add_argument("--absolute-tolerance",type=float,default=0.05)

    pbs=sub.add_parser("pbs",help="OpenPBS render/submit")
    pbss=pbs.add_subparsers(dest="pbs_command",required=True)
    sp=pbss.add_parser("render");_add_root(sp);sp.add_argument("--preview",action="store_true")
    sp=pbss.add_parser("submit");_add_root(sp);sp.add_argument("--yes",action="store_true")

    sp=sub.add_parser("dashboard",help="Serve a local read-only web dashboard")
    _add_root(sp);sp.add_argument("--host",default="127.0.0.1");sp.add_argument("--port",type=int,default=8765);sp.add_argument("--allow-actions",action="store_true");sp.add_argument("--token")

    mig=sub.add_parser("migrate",help="Copy/index legacy campaign artifacts into a new campaign")
    mig.add_argument("legacy_root",type=Path);mig.add_argument("destination",type=Path);mig.add_argument("--link-cifs",action="store_true")
    for flag in ("--apply","--yes","--json"):mig.add_argument(flag,action="store_true")
    mig.add_argument("--plan-file",type=Path)
    up=sub.add_parser("stage-upgrade-data",help="Stage user-owned data from an older manager install")
    up.add_argument("old_install",type=Path);up.add_argument("new_install",type=Path)
    for flag in ("--apply","--yes","--json"):up.add_argument(flag,action="store_true")
    up.add_argument("--plan-file",type=Path)
    sp=sub.add_parser("archive",help="Create a SHA-recorded campaign archive")
    _add_root(sp);sp.add_argument("destination",type=Path);sp.add_argument("--exclude-raw",action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    parser=build_parser();args=parser.parse_args(argv)
    try:
        if args.command=="init": emit(init_campaign(args.path,preset=args.preset,force=args.force));return 0
        if args.command=="migrate": emit(migrate_legacy(args.legacy_root,args.destination,copy_sources=not args.link_cifs,apply=args.apply,yes=args.yes,plan_file=args.plan_file));return 0
        if args.command=="stage-upgrade-data": emit(upgrade_install(args.old_install,args.new_install,apply=args.apply,yes=args.yes,plan_file=args.plan_file));return 0
        config=_cfg(args)
        if args.command=="doctor": report=doctor(config);emit(report);return {"PASS":0,"WARN":1,"FAIL":2}[report["status"]]
        if args.command=="validate": emit({"config":"PASS","inventory":validate_inventory(config)});return 0
        if args.command=="build": emit(build_tasks(config,limit=args.limit));return 0
        if args.command=="plan": emit(plan_campaign(config,n_chunks=args.chunks,refit=not args.no_refit,preview=args.preview));return 0
        if args.command=="inventory": emit(preview_catalog(config,limit=args.limit));return 0
        if args.command=="preview-inputs": emit(preview_inputs(config,limit=args.limit));return 0
        if args.command=="estimate-storage": emit(estimate_storage(config,sample_paths=args.sample,safety_factor=args.safety_factor));return 0
        if args.command=="run":
            if not args.dry_run and not args.confirm_production:
                raise PermissionError("Native engine execution requires --confirm-production. Use --dry-run first.")
            if not args.dry_run and not bool(config.get("campaign.production_authorized",False)):
                raise PermissionError("campaign.production_authorized=false. Set true only after human review/qualification.")
            allocation=parse_duration(args.allocation) if args.allocation else None
            report=run_campaign(config,workers=args.workers,chunk_id=args.chunk,limit=args.limit,dry_run=args.dry_run,retry_failed=args.retry_failed,allocation_seconds=allocation)
            emit(report);return int(report["exit_code"])
        if args.command=="collect":
            from .paths import within
            from .evidence import list_attempts,optional_json
            rows=[]
            for task in iter_tasks(config.root):
                for attempt in list_attempts(config,task):
                    cursor=optional_json(within(attempt,"live/collector_state.json"))
                    if cursor is not None:rows.append({"task_id":task.task_id,"attempt":attempt.name,"cursor":cursor})
            emit({"status":"OBSERVED","read_only":True,"task_local_cursors":rows});return 0
        if args.command=="status":
            report=campaign_status(config,states=set(args.state) if args.state else None,gas=args.gas,mof_contains=args.mof)
            if not args.show_tasks: report.pop("tasks",None)
            if args.json: emit(report)
            else:
                print(f"Campaign: {report['campaign']}\nTasks: {report['total']}\nStates: {report['counts']}\nBy gas: {report['by_gas']}\nEstimated remaining serial hours: {report['estimated_remaining_serial_seconds']/3600:.2f}")
                if args.show_tasks:
                    for r in report.get("tasks",[]): print(f"{r['task_id']}\t{r['mof_id']}\t{r['gas']}\t{r['pressure_bar']} bar\t{r['status_state']}")
            return 0
        if args.command=="task": emit(task_detail(config,args.task_id));return 0
        if args.command=="retry":
            emit(retry_tasks(config,task_ids=args.tasks,states=set(args.state) if args.state else None,reason=args.reason,
                gases=args.gas,chunk_id=args.chunk,max_selected=args.max_selected,apply=args.apply,yes=args.yes,plan_file=args.plan_file));return 0
        if args.command=="recover": emit(recover(config,task_ids=args.tasks));return 0
        if args.command=="claims":
            emit(inspect_claims(config) if args.claims_command=="inspect" else release_stale_claim(config,args.task_id,
                reason=args.reason,apply=args.apply,yes=args.yes,plan_file=args.plan_file,confirm_owner_dead=args.confirm_owner_dead));return 0
        if args.command=="reparse":
            emit(reparse_tasks(config,task_ids=args.tasks,all_attempts=args.all_attempts,
                              apply=args.apply,yes=args.yes,plan_file=args.plan_file,select=not args.no_select));return 0
        if args.command=="export":
            report=export_campaign(config,all_attempts=args.all_attempts,parquet=args.parquet,
                     require_parquet=args.require_parquet,task_ids=args.tasks,states=args.state,gases=args.gas,
                     metadata_csv=args.metadata_csv,metadata_id_column=args.metadata_id_column)
            emit(report);return 3 if report["status"]=="FAIL" else 1 if report["status"]=="WARN" else 0
        if args.command=="audit":
            report=audit_campaign(config,require_all_complete=args.require_all_complete,mode=args.mode,scope=args.scope)
            emit(report);return {"PASS":0,"WARN":1,"FAIL":3,"ERROR":2}[report["status"]]
        if args.command=="compress-logs": emit(compress_completed_outputs(config,delete_raw=args.delete_raw,yes=args.yes));return 0
        if args.command in {"drain","resume"}:
            emit(set_drain(config,args.command=="drain",args.reason,scope=args.scope,run_id=args.run_id,
                apply=args.apply,yes=args.yes,plan_file=args.plan_file));return 0
        if args.command=="qualify":
            emit(qualification_plan(config,per_condition=args.per_condition) if args.qualify_command=="plan" else qualification_report(config));return 0
        if args.command=="qc":
            if args.qc_command=="monotonicity": report=monotonicity_qc(config,tolerance_abs=args.abs_tolerance,tolerance_sigma=args.sigma,input_csv=args.input)
            else: report=cross_engine_qc(config,args.legacy,args.new,relative_tolerance=args.relative_tolerance,absolute_tolerance=args.absolute_tolerance)
            emit(report);return 0 if report["status"]=="PASS" else 5
        if args.command=="pbs": emit(render_pbs(config,preview=args.preview) if args.pbs_command=="render" else submit_pbs(config,yes=args.yes));return 0
        if args.command=="dashboard": serve_dashboard(config,args.host,args.port,allow_actions=args.allow_actions,token=args.token);return 0
        if args.command=="archive": emit(archive_campaign(config.root,args.destination,include_raw=not args.exclude_raw));return 0
        parser.error(f"Unhandled command: {args.command}")
    except (ConfigError,FileNotFoundError,FileExistsError,ValueError,RuntimeError,PermissionError) as exc:
        print(f"ERROR: {exc}",file=sys.stderr);return 2
    return 0
