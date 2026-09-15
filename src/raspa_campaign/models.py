from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class TaskState(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    ENGINE_SUCCEEDED_PARSE_INCOMPLETE = "ENGINE_SUCCEEDED_PARSE_INCOMPLETE"
    PARTIAL_OUTPUT = "PARTIAL_OUTPUT"
    CONTRACT_FAILED = "CONTRACT_FAILED"
    FAILED = "FAILED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass(slots=True)
class Condition:
    gas: str
    temperature_K: float
    pressure_bar: float
    pressure_Pa: int
    seed: int
    replicate: int = 1


@dataclass(slots=True)
class InventoryRecord:
    mof_id: str
    cif_path: str
    descriptors: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Task:
    task_id: str
    task_key: str
    mof_id: str
    cif_path: str
    gas: str
    temperature_K: float
    pressure_bar: float
    pressure_Pa: int
    seed: int
    replicate: int
    model_branch: str = ""
    descriptors: dict[str, Any] = field(default_factory=dict)
    estimated_seconds: float = 0.0
    priority_score: float = 0.0
    priority_reason: str = ""
    chunk_id: int | None = None
    contract_sha256: str = ""
    # Additive P03 identity; old records remain readable, never silently upgraded.
    identity_schema: str = ""
    input_contract: dict[str, Any] = field(default_factory=dict)
    build_config_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Status:
    task_id: str
    state: str = TaskState.PENDING.value
    attempt_no: int = 0
    created_utc: str = field(default_factory=utc_now)
    updated_utc: str = field(default_factory=utc_now)
    hostname: str = ""
    pid: int | None = None
    pbs_job_id: str = ""
    message: str = ""
    exit_code: int | None = None
    wall_seconds: float | None = None
    parse_status: str = "NOT_RUN"
    contract_status: str = "NOT_RUN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParseResult:
    source_files: list[dict[str, Any]] = field(default_factory=list)
    cycle_status: list[dict[str, Any]] = field(default_factory=list)
    final_loadings: list[dict[str, Any]] = field(default_factory=list)
    energy_statistics: list[dict[str, Any]] = field(default_factory=list)
    move_statistics: list[dict[str, Any]] = field(default_factory=list)
    runtime_contract: list[dict[str, Any]] = field(default_factory=list)
    native_timings: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    unknown_sections: list[dict[str, Any]] = field(default_factory=list)
    last_cycle: int | None = None
    loading_primary_mol_kg: float | None = None
    uncertainty_primary_mol_kg: float | None = None
    parse_status: str = "UNPARSED"
    # P01: completion evidence is distinct from progress and diagnostic means.
    final_cycle: int | None = None
    final_cycle_source: str = ""
    final_cycle_line_no: int | None = None
    final_cycle_marker_count: int = 0
    final_loading_source: str = ""
    final_loading_line_no: int | None = None
    completion_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
