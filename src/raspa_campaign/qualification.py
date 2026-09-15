from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .config import CampaignConfig
from .scheduler import plan_campaign
from .status import campaign_status
from .supervisor import run_campaign
from .tasks import iter_tasks


def qualification_plan(config: CampaignConfig, *, per_condition: int = 6) -> dict[str, Any]:
    groups=defaultdict(list)
    for task in iter_tasks(config.root):
        groups[(task.gas,task.temperature_K,task.pressure_bar)].append(task)
    selected=[]
    for key,tasks in sorted(groups.items(),key=str):
        # Spread across LCD/PLD/atom-count proxy instead of taking the first rows only.
        tasks=sorted(tasks,key=lambda t:(float(t.descriptors.get("lcd_A") or 0),float(t.descriptors.get("replicated_framework_atom_count") or 0),t.mof_id))
        if len(tasks)<=per_condition: chosen=tasks
        else:
            idxs=sorted(set(round(i*(len(tasks)-1)/(per_condition-1)) for i in range(per_condition))) if per_condition>1 else [len(tasks)//2]
            chosen=[tasks[i] for i in idxs]
        selected.extend(chosen)
    out=config.root/"plans"/"qualification_tasks.jsonl"
    with out.open("w",encoding="utf-8") as fh:
        for task in selected: fh.write(json.dumps(task.to_dict(),ensure_ascii=False,sort_keys=True)+"\n")
    report={"selected_count":len(selected),"condition_count":len(groups),"per_condition":per_condition,"path":str(out),"note":"Selection spans available pore/runtime proxies. Passing qualification does not generalize beyond the tested input contracts."}
    atomic_write_json(config.root/"reports"/"qualification_plan.json",report)
    return report


def qualification_report(config: CampaignConfig) -> dict[str, Any]:
    path=config.root/"plans"/"qualification_tasks.jsonl"
    if not path.is_file(): raise FileNotFoundError("Create qualification plan first")
    ids={json.loads(line)["task_id"] for line in path.read_text().splitlines() if line.strip()}
    st=campaign_status(config)
    rows=[r for r in st["tasks"] if r["task_id"] in ids]
    states={}
    for r in rows: states[r["status_state"]]=states.get(r["status_state"],0)+1
    passed=len(rows)>0 and states.get("COMPLETE",0)==len(rows)
    report={"status":"PASS" if passed else "NOT_READY","selected_count":len(ids),"observed_count":len(rows),"state_counts":states,"production_release_recommendation":"ELIGIBLE_FOR_HUMAN_REVIEW" if passed else "HOLD","boundary":"This is an engineering qualification of the tested gas, pressure, structure, and input contracts; it is not generic RASPA3 validation."}
    atomic_write_json(config.root/"reports"/"qualification_report.json",report)
    return report
