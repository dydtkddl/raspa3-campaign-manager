from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .hashing import sha256_file
from .models import ParseResult, utc_now

FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
CYCLE_RE = re.compile(r"\b(?:current\s+)?cycle\s*[:=]?\s*(\d+)\b", re.I)
# P01: pinned to the observed RASPA3 final-state heading, not progress text.
FINAL_STATE_RE = re.compile(r"^\s*Final\s+state\s+after\s+(\d+)\s+cycles?\s*:?[ \t]*$", re.I)
PHASE_RE = re.compile(r"\b(initialization|equilibration|production)\b", re.I)
UNIT_RE = re.compile(r"\[([^\]]+)\]")
MEASUREMENT_FLOAT = rf"(?:{FLOAT}|[-+]?(?:nan|inf(?:inity)?))"
PM_RE = re.compile(rf"({MEASUREMENT_FLOAT})\s*(?:\+/-|±|\+\-)\s*({MEASUREMENT_FLOAT})", re.I)
LAST_FLOAT_RE = re.compile(rf"({FLOAT})(?!.*{FLOAT})")
BLOCK_RE = re.compile(rf"\bblock\s*\[?\s*(\d+)\s*\]?\s*[:=]?\s*({FLOAT})", re.I)
TIME_RE = re.compile(rf"\b(total|overall|simulation|sampling[^:]*)?\s*(cpu|wall|elapsed)\s*time\s*[:=]?\s*({FLOAT})\s*(s|sec|seconds|ms|minutes|min|hours|h)?", re.I)
CUTOFF_RE = re.compile(rf"(?P<label>[^:\n]*cut\s*off[^:\n]*)\s*[:=]\s*(?P<value>{FLOAT})\s*(?P<unit>angstrom|å|a)?", re.I)
CHARGE_RE = re.compile(rf"(?P<label>[^:\n]*(?:net\s+)?charge[^:\n]*)\s*[:=]\s*(?P<value>{FLOAT})", re.I)
EWALD_RE = re.compile(rf"(?P<label>[^:\n]*ewald[^:\n]*)\s*[:=]\s*(?P<value>{FLOAT}|auto|none|true|false)", re.I)
MOVE_HEADER_RE = re.compile(r"\b(translation|rotation|reinsertion|swap|widom|cfcmc|volume\s+change|identity\s+change|hybrid)\b.*\b(move|statistics|probability)?\b", re.I)
MOVE_VALUE_RE = re.compile(r"\b(attempted|trials?|total|constructed|accepted|rejected|max(?:imum)?\s+change|cpu\s*time)\b\s*[:=]?\s*" + rf"({FLOAT})", re.I)
ENERGY_RE = re.compile(rf"(?P<label>[^:\n]*energy[^:\n]*)\s*[:=]\s*(?P<value>{FLOAT})(?:\s*(?:\+/-|±)\s*(?P<unc>{FLOAT}))?\s*(?:\[(?P<unit>[^\]]+)\])?", re.I)
MOLECULE_RE = re.compile(r"(?:number\s+of\s+molecules|molecule\s+count)\s*[:=]\s*(\d+)", re.I)
WARNING_RE = re.compile(r"\b(warning|error|fatal|nan|inf|segmentation|exception|failed|overflow|underflow)\b", re.I)


def normalize_space(text: str) -> str:
    return " ".join(text.strip().split())


def normalize_unit(unit: str | None) -> str:
    if not unit:
        return "unknown"
    s = normalize_space(unit).lower()
    repl = {
        "mol/kg-framework": "mol_kg_framework",
        "mol/kg framework": "mol_kg_framework",
        "mol kg^-1": "mol_kg_framework",
        "mg/g-framework": "mg_g_framework",
        "mg/g framework": "mg_g_framework",
        "molecules/uc": "molecules_uc",
        "molecules/unit cell": "molecules_uc",
        "molecules/cell": "molecules_cell",
        "k": "K",
    }
    if s in repl:
        return repl[s]
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _loading_kind(line: str) -> str | None:
    low = line.lower()
    if "loading" not in low and "uptake" not in low:
        return None
    if "excess" in low:
        return "excess"
    if "absolute" in low or "abs." in low or "abs " in low:
        return "absolute"
    return "unspecified"


def _loading_record(line: str, *, cycle: int | None, phase: str, line_no: int, source: str) -> dict[str, Any] | None:
    kind = _loading_kind(line)
    if kind is None:
        return None
    unit_match = UNIT_RE.search(line)
    unit = normalize_unit(unit_match.group(1) if unit_match else None)
    pm = PM_RE.search(line)
    if pm:
        value = float(pm.group(1)); unc = float(pm.group(2))
    else:
        nonfinite = re.search(r"(?<![A-Za-z0-9_])[-+]?(?:nan|inf(?:inity)?)(?![A-Za-z0-9_])", line, re.I)
        if nonfinite:
            value = float(nonfinite.group(0)); unc = None
        else:
            nums = re.findall(FLOAT, line)
            if not nums:
                return None
            # Diagnostic status values keep the existing numeric selection rule.
            value = float(nums[-1]); unc = None
    role = "final_mean" if re.search(r"average|mean|final", line, re.I) else "status"
    return {
        "cycle": cycle,
        "phase": phase,
        "loading_kind": kind,
        "record_role": role,
        "unit": unit,
        "value": value,
        "uncertainty": unc,
        "line_no": line_no,
        "source": source,
        "raw": normalize_space(line),
    }


def _contract_kind(label: str) -> tuple[str, str]:
    low = normalize_space(label).lower()
    if "framework" in low and "molecule" in low and ("vdw" in low or "van der waals" in low):
        return "framework_molecule_vdw_cutoff_A", "cutoff"
    if low.count("molecule") >= 2 and ("vdw" in low or "van der waals" in low):
        return "molecule_molecule_vdw_cutoff_A", "cutoff"
    if "coulomb" in low:
        return "coulomb_cutoff_A", "cutoff"
    if "framework" in low and "charge" in low:
        return "framework_net_charge_e", "charge"
    if ("component" in low or "guest" in low or "molecule" in low) and "charge" in low:
        return "component_net_charge_e", "charge"
    if "system" in low and "charge" in low:
        return "system_net_charge_e", "charge"
    return re.sub(r"[^a-z0-9]+", "_", low).strip("_"), "other"


def _flatten_json(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, val in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_json(val, name)
    elif isinstance(value, list):
        for idx, val in enumerate(value):
            yield from _flatten_json(val, f"{prefix}[{idx}]")
    else:
        yield prefix, value


def parse_text(path: Path | str, *, expected_final_cycle: int | None = None) -> ParseResult:
    p = Path(path)
    result = ParseResult(parse_status="PARSING")
    result.source_files.append({"path": str(p), "size_bytes": p.stat().st_size, "sha256": sha256_file(p), "role": "raspa3_output_text"})
    current_cycle: int | None = None
    phase = "unknown"
    current_move = "unknown"
    status_group: list[dict[str, Any]] = []
    final_candidates: list[dict[str, Any]] = []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        recognized = False
        pm = PHASE_RE.search(stripped)
        if pm and ("cycle" in stripped.lower() or "phase" in stripped.lower() or stripped.lower().startswith(pm.group(1).lower())):
            phase = pm.group(1).lower()
            recognized = True
        final_marker = FINAL_STATE_RE.fullmatch(stripped)
        cm = CYCLE_RE.search(stripped)
        if final_marker:
            result.final_cycle_marker_count += 1
            result.final_cycle = int(final_marker.group(1))
            result.final_cycle_source = str(p)
            result.final_cycle_line_no = line_no
            current_cycle = result.final_cycle
            result.last_cycle = result.final_cycle
            phase = "production"
            recognized = True
        elif cm:
            current_cycle = int(cm.group(1))
            if result.final_cycle is None:
                result.last_cycle = max(result.last_cycle or 0, current_cycle)
            elif "PROGRESS_AFTER_FINAL_MARKER" not in result.completion_issues:
                result.completion_issues.append("PROGRESS_AFTER_FINAL_MARKER")
            recognized = True
        mol = MOLECULE_RE.search(stripped)
        if mol:
            status_group.append({"cycle": current_cycle, "phase": phase, "metric": "molecule_count", "value": int(mol.group(1)), "unit": "count", "line_no": line_no, "source": str(p)})
            recognized = True
        loading = _loading_record(stripped, cycle=current_cycle, phase=phase, line_no=line_no, source=str(p))
        if loading:
            recognized = True
            if loading["record_role"] == "final_mean":
                final_candidates.append(loading)
            else:
                status_group.append(loading)
        block = BLOCK_RE.search(stripped)
        if block and ("loading" in stripped.lower() or "uptake" in stripped.lower()):
            recognized = True
            result.final_loadings.append({"block_index": int(block.group(1)), "value": float(block.group(2)), "unit": normalize_unit(UNIT_RE.search(stripped).group(1) if UNIT_RE.search(stripped) else None), "loading_kind": _loading_kind(stripped) or "unspecified", "source": str(p), "line_no": line_no})
        mh = MOVE_HEADER_RE.search(stripped)
        if mh and len(stripped) < 160:
            current_move = normalize_space(mh.group(1)).lower().replace(" ", "_")
            recognized = True
        move_vals = list(MOVE_VALUE_RE.finditer(stripped))
        if move_vals and current_move != "unknown":
            recognized = True
            record: dict[str, Any] = {"cycle": current_cycle, "phase": phase, "move_type": current_move, "source": str(p), "line_no": line_no, "raw": normalize_space(stripped)}
            for m in move_vals:
                key = normalize_space(m.group(1)).lower().replace(" ", "_")
                record[key] = float(m.group(2))
            result.move_statistics.append(record)
        em = ENERGY_RE.search(stripped)
        if em and not any(word in stripped.lower() for word in ("probability", "histogram", "cutoff")):
            recognized = True
            label = normalize_space(em.group("label"))
            result.energy_statistics.append({"cycle": current_cycle, "phase": phase, "energy_term": re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_"), "value": float(em.group("value")), "uncertainty": float(em.group("unc")) if em.group("unc") else None, "unit": normalize_unit(em.group("unit")), "source": str(p), "line_no": line_no, "raw": normalize_space(stripped)})
        tm = TIME_RE.search(stripped)
        if tm:
            recognized = True
            value = float(tm.group(3)); unit = (tm.group(4) or "s").lower()
            factor = 0.001 if unit == "ms" else 60 if unit in {"m", "min", "minutes"} else 3600 if unit in {"h", "hours"} else 1
            result.native_timings.append({"timing_label": normalize_space(" ".join(x for x in tm.groups()[:2] if x)), "seconds": value * factor, "source": str(p), "line_no": line_no, "raw": normalize_space(stripped)})
        co = CUTOFF_RE.search(stripped)
        if co:
            recognized = True
            field, _ = _contract_kind(co.group("label"))
            result.runtime_contract.append({"field": field, "reported_value": float(co.group("value")), "unit": "A", "source": str(p), "line_no": line_no, "raw": normalize_space(stripped)})
        ch = CHARGE_RE.search(stripped)
        if ch:
            recognized = True
            field, _ = _contract_kind(ch.group("label"))
            result.runtime_contract.append({"field": field, "reported_value": float(ch.group("value")), "unit": "e", "source": str(p), "line_no": line_no, "raw": normalize_space(stripped)})
        ew = EWALD_RE.search(stripped)
        if ew:
            recognized = True
            field = re.sub(r"[^a-z0-9]+", "_", normalize_space(ew.group("label")).lower()).strip("_")
            raw_val = ew.group("value")
            try:
                val: Any = float(raw_val)
            except ValueError:
                val = raw_val.lower()
            result.runtime_contract.append({"field": field, "reported_value": val, "unit": "native", "source": str(p), "line_no": line_no, "raw": normalize_space(stripped)})
        if WARNING_RE.search(stripped):
            recognized = True
            severity = "ERROR" if re.search(r"fatal|error|segmentation|exception|failed", stripped, re.I) else "WARNING"
            result.events.append({"timestamp_utc": utc_now(), "severity": severity, "event_type": "NATIVE_MESSAGE", "source": str(p), "line_no": line_no, "raw_message": normalize_space(stripped)})
        if not recognized and (":" in stripped or stripped.endswith(":") or re.match(r"^[A-Z][A-Za-z0-9 /_.()\-]{2,80}$", stripped)):
            result.unknown_sections.append({
                "cycle": current_cycle, "phase": phase, "source": str(p), "line_no": line_no,
                "raw": normalize_space(stripped), "event_type": "UNCLASSIFIED_NATIVE_OUTPUT"
            })
    result.cycle_status = status_group
    result.final_loadings.extend(final_candidates)
    for row in final_candidates:
        row["final_section_confirmed"] = (
            result.final_cycle_line_no is not None and row["line_no"] > result.final_cycle_line_no
        )
    primary = [r for r in final_candidates if r.get("loading_kind") == "absolute" and r.get("unit") == "mol_kg_framework"]
    if result.final_cycle is not None:
        # Earlier means remain diagnostic rows, not authority for completion.
        primary = [r for r in primary if r["final_section_confirmed"]]
        if result.final_cycle_marker_count != 1:
            result.completion_issues.append("MULTIPLE_FINAL_MARKERS")
        if len(primary) > 1:
            result.completion_issues.append("MULTIPLE_FINAL_PRIMARY_ROWS")
    if primary:
        chosen = primary[-1]
        result.loading_primary_mol_kg = chosen["value"]
        result.uncertainty_primary_mol_kg = chosen.get("uncertainty")
        if result.final_cycle is not None:
            result.final_loading_source = chosen["source"]
            result.final_loading_line_no = chosen["line_no"]
    if expected_final_cycle is not None and result.final_cycle != expected_final_cycle:
        result.events.append({"timestamp_utc": utc_now(), "severity": "ERROR", "event_type": "FINAL_CYCLE_MISSING" if result.final_cycle is None else "LAST_CYCLE_MISMATCH", "source": str(p), "expected": expected_final_cycle, "observed": result.final_cycle})
    result.parse_status = "PARSED" if result.final_loadings or result.cycle_status else "PARSED_NO_PROPERTIES"
    return result


def merge_parse_results(results: list[ParseResult]) -> ParseResult:
    """Keep diagnostics, but never assemble completion from different files.

    P01 supports one unambiguous final-state text output per task. Byte-identical
    copies are deduplicated; distinct final outputs require a later system-aware
    adapter and fail completion instead of selecting by filename or largest cycle.
    """
    unique: list[ParseResult] = []
    seen: set[tuple[str, ...]] = set()
    for result in results:
        identity = tuple(str(x.get("sha256") or x.get("path")) for x in result.source_files)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        unique.append(result)

    merged = ParseResult(parse_status="UNPARSED")
    for result in unique:
        merged.source_files.extend(result.source_files)
        merged.cycle_status.extend(result.cycle_status)
        merged.final_loadings.extend(result.final_loadings)
        merged.energy_statistics.extend(result.energy_statistics)
        merged.move_statistics.extend(result.move_statistics)
        merged.runtime_contract.extend(result.runtime_contract)
        merged.native_timings.extend(result.native_timings)
        merged.events.extend(result.events)
        merged.unknown_sections.extend(result.unknown_sections)
        if result.last_cycle is not None:
            merged.last_cycle = max(merged.last_cycle or 0, result.last_cycle)
        if result.loading_primary_mol_kg is not None:
            merged.loading_primary_mol_kg = result.loading_primary_mol_kg
            merged.uncertainty_primary_mol_kg = result.uncertainty_primary_mol_kg

    finals = [r for r in unique if r.final_cycle is not None]
    if len(finals) == 1:
        authoritative = finals[0]
        for field in (
            "last_cycle", "final_cycle", "final_cycle_source", "final_cycle_line_no",
            "final_cycle_marker_count", "final_loading_source", "final_loading_line_no",
            "loading_primary_mol_kg", "uncertainty_primary_mol_kg",
        ):
            setattr(merged, field, getattr(authoritative, field))
        merged.completion_issues = list(authoritative.completion_issues)
    elif len(finals) > 1:
        merged.loading_primary_mol_kg = None
        merged.uncertainty_primary_mol_kg = None
        merged.completion_issues = ["MULTIPLE_DISTINCT_FINAL_OUTPUTS"]
    merged.parse_status = "PARSED" if any(r.parse_status.startswith("PARSED") for r in unique) else "UNPARSED"
    return merged


def parse_output_json(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    flat = []
    for key, value in _flatten_json(data):
        if isinstance(value, (str, int, float, bool)) or value is None:
            flat.append({"path": key, "value": value})
    return {"source": str(p), "sha256": sha256_file(p), "flat": flat}


def parse_attempt(attempt_dir: Path | str, output_globs: list[str], *, expected_final_cycle: int | None = None, json_globs: list[str] | None = None) -> tuple[ParseResult, list[dict[str, Any]]]:
    attempt = Path(attempt_dir)
    work = attempt / "work"
    paths: list[Path] = []
    for pattern in output_globs:
        paths.extend(work.glob(pattern))
    paths = sorted(set(p for p in paths if p.is_file()))
    results = [parse_text(p, expected_final_cycle=expected_final_cycle) for p in paths]
    merged = merge_parse_results(results)
    json_records = []
    for pattern in json_globs or []:
        for p in sorted(set(work.glob(pattern))):
            if p.is_file():
                try:
                    json_records.append(parse_output_json(p))
                except (json.JSONDecodeError, OSError) as exc:
                    merged.events.append({"timestamp_utc": utc_now(), "severity": "WARNING", "event_type": "JSON_OUTPUT_PARSE_FAILED", "source": str(p), "message": str(exc)})
    if not paths:
        merged.events.append({"timestamp_utc": utc_now(), "severity": "ERROR", "event_type": "RESULT_FILE_NOT_FOUND", "source": str(work)})
        merged.parse_status = "NO_RESULT_FILE"
    return merged, json_records
