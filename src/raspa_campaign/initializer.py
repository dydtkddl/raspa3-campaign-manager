from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text
from .presets import get_preset


def _toml_list(values: list[Any]) -> str:
    parts = []
    for value in values:
        if isinstance(value, str): parts.append(repr(value).replace("'", '"'))
        elif isinstance(value, bool): parts.append("true" if value else "false")
        else: parts.append(str(value))
    return "[" + ", ".join(parts) + "]"


def default_config(name: str, preset_name: str = "single_h2_pilot") -> str:
    preset = get_preset(preset_name)
    lines = [f'''[campaign]
name = "{name}"
schema_version = 2
model_branch = "UNQUALIFIED_REPLACE_ME"
production_authorized = false

[engine]
binary = "raspa3"
version_args = ["--version"]
command = ["${{ENGINE}}"]

[inputs]
inventory = "inventory/inventory.csv"
id_column = "mof_id"
cif_column = "cif_path"
template_dir = "templates"
staging_mode = "copy"
render_suffixes = [".json", ".txt", ".toml", ".yaml", ".yml"]

[cycles]
initialization = 1000
production = 2000
print_every = 500

[execution]
workers = 1
threads_per_task = 1
collector_interval_seconds = 10
timeout_seconds = 0
kill_grace_seconds = 30
drain_safety_seconds = 900
max_attempts = 3
auto_retry = false
use_gnu_time = true
output_globs = ["output/output_*.txt", "output_*.txt"]
json_output_globs = ["output/output_*.json", "output_*.json"]

[scheduling]
strategy = "lpt"
priority_mode = "soft"
gas_priority = {_toml_list(preset['gas_priority'])}
base_seconds = 600.0
workers_per_chunk = 1
target_chunk_walltime = "1h"
utilization_target = 0.90
number_of_chunks = 1

[parsing]
preserve_unknown_sections = true
crosscheck_json = true
strict_final_cycle = true

[contracts]
require_result_file = true
require_finite_loading = true
require_final_loading = true
require_expected_cycle = true
charge_tolerance = 1.0e-8
cutoff_tolerance_A = 1.0e-6
cbmc_constructed_positive = false

[contracts.requested_cutoffs_A]
# Fill only after qualification, using exact native-output field names.
# framework_molecule_vdw_cutoff_A = 12.0
# molecule_molecule_vdw_cutoff_A = 12.0
# coulomb_cutoff_A = 14.0

[pbs]
enabled = false
queue = ""
project = ""
job_name = "raspa_campaign"
ncpus = 1
memory = ""
walltime = "01:00:00"
array_concurrency = 1
signal_before_end_seconds = 300
extra_directives = []
module_commands = []
''']
    for condition in preset["conditions"]:
        lines.append(f'''[[matrix.conditions]]
gas = "{condition['gas']}"
temperatures_K = {_toml_list(condition['temperatures_K'])}
pressures_bar = {_toml_list(condition['pressures_bar'])}
seeds = {_toml_list(condition['seeds'])}
replicates = {int(condition.get('replicates', 1))}
''')
    return "\n".join(lines)


def simulation_template() -> str:
    return '''{
  "SimulationType": "MonteCarlo",
  "NumberOfCycles": ${PRODUCTION_CYCLES},
  "NumberOfInitializationCycles": ${INITIALIZATION_CYCLES},
  "NumberOfEquilibrationCycles": 0,
  "PrintEvery": ${PRINT_EVERY},
  "RandomSeed": ${SEED},
  "Systems": [
    {
      "Type": "Framework",
      "Name": "${MOF_ID}",
      "NumberOfUnitCells": [1, 1, 1],
      "ExternalTemperature": ${TEMPERATURE_K},
      "ExternalPressure": ${PRESSURE_PA},
      "ChargeMethod": "Ewald"
    }
  ],
  "Components": [
    {
      "Name": "${GAS}",
      "MoleculeDefinition": "REPLACE_WITH_QUALIFIED_DEFINITION",
      "TranslationProbability": 0.5,
      "RotationProbability": 0.5,
      "ReinsertionProbability": 0.5,
      "SwapProbability": 1.0,
      "CreateNumberOfMolecules": 0
    }
  ]
}
'''


def init_campaign(root: Path | str, *, preset: str = "single_h2_pilot", force: bool = False) -> dict[str, Any]:
    path = Path(root).expanduser().resolve()
    if path.exists() and any(path.iterdir()) and not force:
        raise FileExistsError(f"Campaign directory is not empty: {path}")
    for d in ("inventory", "templates", "tasks", "plans", "chunks", "reports", "exports", "live", "logs", "control", "pbs", "archive"):
        (path / d).mkdir(parents=True, exist_ok=True)
    atomic_write_text(path / "campaign.toml", default_config(path.name, preset))
    atomic_write_text(path / "templates" / "simulation.json", simulation_template())
    atomic_write_text(path / "templates" / "README_TEMPLATE_INPUTS.txt", "Replace the placeholder molecule and force-field definitions with an already qualified RASPA3 input contract before real execution.\n")
    inv = path / "inventory" / "inventory.csv"
    with inv.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["mof_id", "cif_path", "pld_A", "lcd_A", "void_fraction", "replicated_framework_atom_count", "prior_wall_seconds", "prior_pressure_bar", "prior_uptake_mol_kg"])
        writer.writerow(["REPLACE_ME", "/absolute/path/REPLACE_ME.cif", "", "", "", "", "", "", ""])
    atomic_write_text(path / "README_FIRST.md", f"# {path.name}\n\n1. Replace the inventory row.\n2. Replace the example simulation input with a qualified RASPA3 contract.\n3. Set an absolute engine path.\n4. Run `raspa-campaign doctor --root {path}`.\n5. Build, plan, dry-run, then execute a one-task pilot.\n")
    return {"root": str(path), "preset": preset, "config": str(path / "campaign.toml")}
