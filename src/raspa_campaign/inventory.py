from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Iterable

from .config import CampaignConfig
from .models import InventoryRecord
from .paths import no_symlinks, read_regular, safe_name


class InventoryError(ValueError):
    pass


def _coerce(value: str) -> Any:
    s = value.strip()
    if s == "":
        return None
    low = s.lower()
    if low in {"true", "false"}:
        return low == "true"
    try:
        n = float(s)
        return int(n) if n.is_integer() and not any(c in s.lower() for c in (".", "e")) else n
    except ValueError:
        return s


def inventory_path(config: CampaignConfig) -> Path:
    raw = Path(str(config.get("inputs.inventory", "inventory/inventory.csv")))
    return no_symlinks(raw if raw.is_absolute() else config.root / raw)


def _load_csv_inventory(config: CampaignConfig, *, require_cif: bool = True) -> list[InventoryRecord]:
    path = inventory_path(config)
    if not path.is_file():
        raise InventoryError(f"Inventory not found: {path}")
    id_col = str(config.get("inputs.id_column", "mof_id"))
    cif_col = str(config.get("inputs.cif_column", "cif_path"))
    rows: list[InventoryRecord] = []
    seen: set[str] = set()
    import io
    with io.StringIO(read_regular(path).decode("utf-8-sig"), newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or id_col not in reader.fieldnames or cif_col not in reader.fieldnames:
            raise InventoryError(f"Inventory requires columns {id_col!r} and {cif_col!r}")
        for line_no, row in enumerate(reader, start=2):
            mof_id = (row.get(id_col) or "").strip()
            cif_raw = (row.get(cif_col) or "").strip()
            if not mof_id:
                raise InventoryError(f"Empty {id_col} at line {line_no}")
            safe_name(mof_id, "mof_id")
            if not cif_raw:
                raise InventoryError(f"Empty CIF path at line {line_no}")
            if mof_id in seen:
                raise InventoryError(f"Duplicate MOF id at line {line_no}: {mof_id}")
            seen.add(mof_id)
            cif = Path(cif_raw).expanduser()
            if not cif.is_absolute():
                cif = path.parent / cif
            cif = no_symlinks(cif)
            if require_cif and not cif.is_file():
                raise InventoryError(f"CIF not found for {mof_id}: {cif}")
            descriptors = {k: _coerce(v or "") for k, v in row.items() if k not in {id_col, cif_col}}
            rows.append(InventoryRecord(mof_id=mof_id, cif_path=str(cif), descriptors=descriptors))
    if not rows:
        raise InventoryError(f"Inventory is empty: {path}")
    return rows


def load_inventory(config: CampaignConfig, *, require_cif: bool = True) -> list[InventoryRecord]:
    from .discovery import discover_cifs, read_descriptor_table, filter_records
    if config.get("inputs.cif_dir") is not None:
        records, _report = discover_cifs(config)
    else:
        records = _load_csv_inventory(config, require_cif=require_cif)
        metadata, _report = read_descriptor_table(config)
        if metadata:
            for record in records:
                for key, value in metadata.get(record.mof_id, {}).items():
                    prior = record.descriptors.get(key)
                    if prior is not None and prior != value:
                        raise InventoryError(f"Conflicting descriptor values: {record.mof_id}/{key}")
                    record.descriptors[key] = value
    return filter_records(config, records, config.get("selection"), context="selection")


def validate_inventory(config: CampaignConfig) -> dict[str, Any]:
    records = load_inventory(config, require_cif=False)
    missing = [r for r in records if not Path(r.cif_path).is_file()]
    descriptor_names = sorted({k for r in records for k in r.descriptors})
    numeric_coverage: dict[str, int] = {}
    for name in descriptor_names:
        numeric_coverage[name] = sum(
            1 for r in records
            if isinstance(r.descriptors.get(name), (int, float))
            and not (isinstance(r.descriptors.get(name), float) and math.isnan(r.descriptors[name]))
        )
    return {
        "inventory": str(config.get("inputs.cif_dir") or inventory_path(config)),
        "source_mode": "cif_directory" if config.get("inputs.cif_dir") is not None else "inventory_csv",
        "record_count": len(records),
        "missing_cif_count": len(missing),
        "missing_cifs": [{"mof_id": r.mof_id, "cif_path": r.cif_path} for r in missing],
        "descriptor_columns": descriptor_names,
        "numeric_coverage": numeric_coverage,
        "status": "PASS" if not missing else "FAIL",
    }


def write_inventory(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        raise InventoryError("Cannot write an empty inventory")
    fields = list(dict.fromkeys(k for row in rows for k in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
