"""P04: explicit CIF directory/metadata input and reproducible subset selection.

No pore calculation, engine execution, filesystem writes, or chemical inference.
Identifiers are filename stems; only a final .cif suffix is normalized in lists.
"""
from __future__ import annotations

import csv
import io
import math
import os
from pathlib import Path
from typing import Any

from .config import CampaignConfig, ConfigError
from .hashing import canonical_sha256, sha256_bytes
from .models import InventoryRecord
from .paths import no_symlinks, read_regular, safe_name


def path_from(config: CampaignConfig, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError('Input path must be a nonempty string')
    path = Path(raw).expanduser()
    return no_symlinks(path if path.is_absolute() else config.root / path)


def number(value: Any, label: str, *, allow_missing: bool = False) -> float | None:
    if allow_missing and value in (None, ''):
        return None
    if isinstance(value, bool):
        raise ConfigError(label + ': a finite nonnegative number is required')
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ConfigError(label + ': a finite nonnegative number is required') from None
    if not math.isfinite(n) or n < 0:
        raise ConfigError(label + ': a finite nonnegative number is required')
    return n


def identifier(value: str) -> str:
    name = str(value).strip()
    if name.lower().endswith('.cif'):
        name = name[:-4]
    return safe_name(name, 'MOF identifier')


def list_ids(config: CampaignConfig, spec: dict, key: str) -> set[str] | None:
    values = spec.get(key)
    file = spec.get(key + '_file')
    if values is not None and file is not None:
        raise ConfigError(f'{key} and {key}_file are mutually exclusive')
    if file is not None:
        values = [line.strip() for line in read_regular(path_from(config, file), maximum_bytes=32*1024*1024)
                  .decode('utf-8-sig').splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if values is None:
        return None
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ConfigError(key + ': string list required')
    ids = [identifier(v) for v in values]
    if len(ids) != len(set(ids)):
        raise ConfigError(key + ': duplicate normalized identifiers')
    if key == 'include' and not ids:
        raise ConfigError('Explicit include list is empty')
    return set(ids)


def read_descriptor_table(config: CampaignConfig) -> tuple[dict[str, dict], dict]:
    raw_path = config.get('inputs.descriptor_csv')
    if raw_path is None:
        return {}, {'descriptor_csv': None, 'pld_calculated': False}
    path = path_from(config, raw_path)
    raw = read_regular(path, maximum_bytes=256*1024*1024)
    key = config.get('inputs.descriptor_id_column', 'mof_id')
    mapping = config.get('inputs.descriptor_columns', {})
    if not isinstance(mapping, dict) or not mapping or any(not isinstance(k,str) or not isinstance(v,str) for k,v in mapping.items()):
        raise ConfigError('inputs.descriptor_columns must explicitly map descriptor names to CSV column names')
    result = {}
    with io.StringIO(raw.decode('utf-8-sig'), newline='') as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or key not in header or set(mapping.values()) - set(header):
            raise ConfigError('Descriptor CSV has missing/duplicate columns')
        for line_no, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ConfigError(f'Descriptor CSV ragged row {line_no}')
            name = identifier(row[key])
            if name in result:
                raise ConfigError('Duplicate descriptor identifier: ' + name)
            result[name] = {}
            for dst, src in mapping.items():
                v = row[src].strip()
                if not v:
                    result[name][dst] = None
                else:
                    try:
                        n = float(v)
                    except ValueError:
                        result[name][dst] = v
                    else:
                        if not math.isfinite(n):
                            raise ConfigError(f'Nonfinite descriptor {name}/{src}')
                        result[name][dst] = n
    return result, {'descriptor_csv':str(path), 'sha256':sha256_bytes(raw), 'rows':len(result),
                    'column_map':mapping, 'id_column':key, 'pld_calculated':False}


def discover_cifs(config: CampaignConfig) -> tuple[list[InventoryRecord], dict]:
    if config.get('inputs.inventory') is not None:
        raise ConfigError('Specify inputs.cif_dir OR inputs.inventory, not both')
    root = path_from(config, config.get('inputs.cif_dir'))
    if not root.is_dir():
        raise ConfigError('CIF directory not found: ' + str(root))
    recursive = config.get('inputs.cif_recursive', False)
    maximum = config.get('inputs.max_cifs', 100000)
    if type(recursive) is not bool or type(maximum) is not int or not 1 <= maximum <= 1000000:
        raise ConfigError('cif_recursive must be boolean; max_cifs must be 1..1000000')
    paths = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs.sort(); names.sort()
        if recursive:
            for directory in dirs:
                no_symlinks(Path(base)/directory)
        else:
            dirs[:] = []
        for name in names:
            if Path(name).suffix.lower() != '.cif':
                continue
            p = no_symlinks(Path(base)/name)
            if not p.is_file():
                raise ConfigError('Nonregular CIF: ' + str(p))
            paths.append(p)
            if len(paths) > maximum:
                raise ConfigError('CIF count exceeds inputs.max_cifs; no files silently truncated')
    paths.sort(key=lambda p:(p.stem.casefold(),p.stem,p.relative_to(root).as_posix()))
    if not paths:
        raise ConfigError('No .cif files in the selected directory')
    seen = set(); records = []
    metadata, meta_info = read_descriptor_table(config)
    missing = []
    for p in paths:
        name = safe_name(p.stem, 'CIF stem')
        if name.casefold() in seen:
            raise ConfigError('Duplicate/case-colliding CIF stem: ' + name)
        seen.add(name.casefold())
        if config.get('inputs.descriptor_csv') is not None and name not in metadata:
            missing.append(name)
        records.append(InventoryRecord(name, str(p), dict(metadata.get(name, {}))))
    return records, {'source_mode':'cif_directory', 'cif_dir':str(root), 'recursive':recursive,
                     'discovered_count':len(records), 'descriptor_source':meta_info,
                     'missing_descriptor_ids':missing,
                     'extra_descriptor_ids':sorted(set(metadata)-{r.mof_id for r in records}),
                     'pld_calculated':False}


def filter_records(config: CampaignConfig, records: list[InventoryRecord], spec: dict | None,
                   *, context: str) -> list[InventoryRecord]:
    if spec is None:
        return records
    allowed = {'include','include_file','exclude','exclude_file','pld_min_A','pld_max_A',
               'pld_column','pld_min_inclusive','sample_count','sample_seed'}
    if not isinstance(spec,dict) or set(spec)-allowed:
        raise ConfigError(context + ': unknown selection keys or invalid table')
    names = {r.mof_id for r in records}
    include, exclude = list_ids(config,spec,'include'), list_ids(config,spec,'exclude')
    for label, ids in [('include',include),('exclude',exclude)]:
        if ids is not None and ids-names:
            raise ConfigError(f'{context}.{label}: identifiers not in input: {sorted(ids-names)[:10]}')
    selected = [r for r in records if (include is None or r.mof_id in include) and (not exclude or r.mof_id not in exclude)]
    low = number(spec['pld_min_A'], context+'.pld_min_A') if 'pld_min_A' in spec else None
    high = number(spec['pld_max_A'], context+'.pld_max_A') if 'pld_max_A' in spec else None
    if low is not None and high is not None and low > high:
        raise ConfigError(context + ': pld_min_A exceeds pld_max_A')
    inclusive = spec.get('pld_min_inclusive',False)
    if type(inclusive) is not bool:
        raise ConfigError(context + ': pld_min_inclusive must be boolean')
    if low is not None or high is not None:
        key = spec.get('pld_column', 'pld_A'); kept=[]
        for record in selected:
            pld = number(record.descriptors.get(key), f'{context}: PLD missing/invalid for {record.mof_id}')
            if low is not None and (pld < low if inclusive else pld <= low):
                continue
            if high is not None and pld > high:
                continue
            kept.append(record)
        selected=kept
    count=spec.get('sample_count')
    if count is not None:
        seed=spec.get('sample_seed')
        if type(count) is not int or not 1<=count<=len(selected) or type(seed) is not int or not 0<=seed<=2147483647:
            raise ConfigError(context + ': sample_count within eligible population and explicit integer sample_seed required')
        selected=sorted(selected,key=lambda r:(canonical_sha256(['rcm-subset-p04-v1',seed,r.mof_id]),r.mof_id))[:count]
    if not selected:
        raise ConfigError(context + ': selection is empty; no silent zero-task gas block')
    return selected


def records_for_gas(config: CampaignConfig, records: list[InventoryRecord], gas: str) -> list[InventoryRecord]:
    return filter_records(config, records, config.get(f'gas_profiles.{gas}.selection'), context=f'gas_profiles.{gas}.selection')
