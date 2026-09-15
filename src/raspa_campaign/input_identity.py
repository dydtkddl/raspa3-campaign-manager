"""P03: byte-bound scientific inputs. No RASPA execution or physics repair."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
from typing import Any

from .config import CampaignConfig, ConfigError
from .hashing import canonical_sha256, sha256_bytes
from .models import Task
from .paths import no_symlinks, read_regular, safe_name, tree_files, validate_task_id

IDENTITY_SCHEMA = 'rcm-input-identity-p03-v1'
TOKEN_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


class InputIdentityError(ConfigError):
    pass


def integer(value: Any, key: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2147483647:
        raise InputIdentityError(f'{key}: integer {minimum}..2147483647 required')
    return value


def condition_checked(c: dict[str, Any]) -> dict[str, Any]:
    gas = safe_name(c['gas'], 'gas')
    if any(x in gas for x in '. '):
        raise InputIdentityError('Gas must be a single non-dotted profile name')
    for key in ('temperature_K', 'pressure_bar'):
        if isinstance(c[key], bool) or not isinstance(c[key], (int, float)) or not math.isfinite(float(c[key])) or c[key] <= 0:
            raise InputIdentityError(key + ': a positive finite number is required')
    # Retain existing integer-Pa convention, but do not silently round two inputs.
    pa = Decimal(str(c['pressure_bar'])) * Decimal(100000)
    rounded = pa.to_integral_value()
    if abs(pa - rounded) > Decimal('0.0000001') or rounded < 1:
        raise InputIdentityError('pressure_bar: this profile requires integral Pa; sub-Pa rounding is refused')
    out = dict(gas=gas, temperature_K=float(c['temperature_K']), pressure_bar=float(c['pressure_bar']),
               pressure_Pa=int(rounded), seed=integer(c['seed'], 'seed'), replicate=integer(c['replicate'], 'replicate', 1))
    if 'pressure_Pa' in c and c['pressure_Pa'] != out['pressure_Pa']:
        raise InputIdentityError('Pressure Pa/bar disagreement')
    return out


def cycles_for(config: CampaignConfig, gas: str) -> dict[str, int]:
    return {k: integer(config.get(f'gas_profiles.{gas}.cycles.{k}', config.get('cycles.' + k, default)),
                       'cycles.' + k, minimum)
            for k, default, minimum in [('initialization', 10000, 0), ('production', 20000, 1), ('print_every', 1000, 1)]}


def template_location(config: CampaignConfig, gas: str) -> Path:
    p = Path(str(config.get(f'gas_profiles.{gas}.template_dir', config.get('inputs.template_dir', 'templates')))).expanduser()
    return no_symlinks(p if p.is_absolute() else config.root / p)


def static_context(mof_id: str, c: dict[str, Any], cycles: dict[str, int], branch: str,
                   descriptors: dict[str, Any]) -> dict[str, str]:
    result = {'MOF_ID': mof_id, 'CIF_NAME': mof_id, 'CIF_PATH': mof_id + '.cif', 'GAS': c['gas'],
              'TEMPERATURE_K': format(c['temperature_K'], '.12g'), 'PRESSURE_PA': str(c['pressure_Pa']),
              'PRESSURE_BAR': format(c['pressure_bar'], '.12g'), 'SEED': str(c['seed']), 'REPLICATE': str(c['replicate']),
              'INITIALIZATION_CYCLES': str(cycles['initialization']), 'PRODUCTION_CYCLES': str(cycles['production']),
              'PRINT_EVERY': str(cycles['print_every']), 'MODEL_BRANCH': branch}
    descriptor_tokens = set()
    for key, value in descriptors.items():
        token = 'DESCRIPTOR_' + str(key).upper()
        if not re.fullmatch(r'DESCRIPTOR_[A-Z_][A-Z0-9_]*', token):
            continue
        if token in descriptor_tokens:
            raise InputIdentityError('Descriptor names collide after token normalization')
        descriptor_tokens.add(token)
        if isinstance(value, float) and not math.isfinite(value):
            # Missing non-rendered descriptors do not alter the scientific input.
            continue
        result[token] = '' if value is None else str(value)
    return result


def snapshot_inputs(config: CampaignConfig, record, condition: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes], dict[str, str]]:
    c = condition_checked(condition)
    mof = safe_name(record.mof_id, 'mof_id')
    if config.get('inputs.staging_mode', 'copy') != 'copy':
        raise InputIdentityError('P03 permits copy staging only; linked inputs cannot be sealed independently')
    cycles = cycles_for(config, c['gas'])
    branch = str(config.get(f"gas_profiles.{c['gas']}.model_branch", config.get('campaign.model_branch', 'unspecified')))
    ctx = static_context(mof, c, cycles, branch, record.descriptors)
    suffixes = config.get('inputs.render_suffixes', ['.json', '.txt', '.toml', '.yaml', '.yml'])
    if not isinstance(suffixes, list) or any(not isinstance(s, str) or not re.fullmatch(r'\.[A-Za-z0-9]+', s) for s in suffixes):
        raise InputIdentityError('inputs.render_suffixes must be a list of filename suffixes')
    suffixes = sorted(set(s.lower() for s in suffixes))
    template = template_location(config, c['gas'])
    files = tree_files(template)
    if not files:
        raise InputIdentityError('Empty template directory')
    sources, rendered, source_paths, bindings = [], {}, {}, {}
    for path in files:
        rel = path.relative_to(template).as_posix()
        if rel == mof + '.cif':
            raise InputIdentityError('Template/CIF destination collision: ' + rel)
        raw = read_regular(path)
        out = raw
        if path.suffix.lower() in suffixes:
            text = raw.decode('utf-8')
            used = TOKEN_RE.findall(text)
            unknown = sorted(set(used) - set(ctx))
            if unknown:
                raise InputIdentityError('Unbound or non-deterministic input placeholder: ' + ', '.join(unknown))
            bindings.update({k: ctx[k] for k in used})
            text = TOKEN_RE.sub(lambda m: ctx[m.group(1)], text)
            if '${' in text:
                raise InputIdentityError('Malformed or nested template placeholder')
            out = text.encode('utf-8')
        # All JSON, rendered or literal, must be parseable before any destination writes.
        if path.suffix.lower() == '.json':
            try:
                json.loads(out.decode('utf-8'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
            except (ValueError, UnicodeError) as exc:
                raise InputIdentityError('Invalid staged JSON: ' + rel) from exc
        rendered[rel] = out
        sources.append({'role': 'template', 'path': rel, 'size_bytes': len(raw), 'sha256': sha256_bytes(raw)})
        source_paths[rel] = str(path)
    if 'simulation.json' not in rendered:
        raise InputIdentityError('Template must contain simulation.json')
    cif = no_symlinks(Path(record.cif_path))
    raw = read_regular(cif)
    if not raw:
        raise InputIdentityError('Empty CIF input')
    name = mof + '.cif'
    rendered[name] = raw
    source_paths[name] = str(cif)
    sources.append({'role': 'cif', 'path': name, 'size_bytes': len(raw), 'sha256': sha256_bytes(raw)})
    from .raspa_settings import apply_profile
    profile_recipe = apply_profile(config, record, c, cycles, rendered, sources, source_paths)
    if files != tree_files(template):
        raise InputIdentityError('Template file set changed during input snapshot')
    command = config.get(f"gas_profiles.{c['gas']}.engine_command", config.get('engine.command', ['${ENGINE}']))
    # Bind deterministic command substitutions too; do not expand arbitrary host secrets.
    argv = [command] if isinstance(command, str) else command
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
        raise InputIdentityError('engine.command must be a string or nonempty string list')
    runtime_tokens = {'ENGINE','CAMPAIGN_ROOT','TASK_ID','TASK_KEY','ATTEMPT_NO','ATTEMPT_DIR','WORK_DIR'}
    for arg in argv:
        used = set(TOKEN_RE.findall(arg))
        if used - set(ctx) - runtime_tokens:
            raise InputIdentityError('Unbound engine.command placeholder')
        bindings.update({k: ctx[k] for k in used if k in ctx})
    # The command declaration is part of the profile; executable bytes are recorded at launch.
    contract = {'schema': IDENTITY_SCHEMA, 'mof_id': mof, **c, 'cycles': cycles, 'model_branch': branch,
                'engine_profile': {'profile': config.get('engine.profile', ''),
                                   'expected_version': config.get('engine.expected_version', ''),
                                   'expected_sha256': config.get('engine.expected_sha256', ''), 'command': command},
                'render_suffixes': suffixes, 'template_bindings': bindings,
                'source_files': sorted(sources, key=lambda r: (r['role'], r['path'])),
                'staged_files': [{'path': rel, 'sha256': sha256_bytes(raw), 'size_bytes': len(raw)} for rel, raw in sorted(rendered.items())]}
    if profile_recipe is not None:
        contract['raspa_profile'] = profile_recipe
    return contract, rendered, source_paths


def validate_task_record(task: Task, *, require_identity: bool = False) -> None:
    validate_task_id(task.task_id)
    if task.identity_schema != IDENTITY_SCHEMA:
        if require_identity or task.identity_schema or task.input_contract:
            raise InputIdentityError('Legacy/unknown input identity: preserve old tasks; build a fresh P03 campaign, no automatic migration')
        return
    contract = task.input_contract
    if not isinstance(contract, dict) or contract.get('schema') != IDENTITY_SCHEMA:
        raise InputIdentityError('Invalid input contract schema')
    digest = canonical_sha256(contract)
    if task.contract_sha256 != digest or task.task_id != 'task_' + digest[:20]:
        raise InputIdentityError('Task input contract hash/ID mismatch')
    c = condition_checked({k: getattr(task, k) for k in ('gas','temperature_K','pressure_bar','pressure_Pa','seed','replicate')})
    expected_key = '__'.join([task.mof_id, c['gas'], f"{c['temperature_K']:g}K", f"{c['pressure_bar']:g}bar", f"seed{c['seed']}", f"rep{c['replicate']}"])
    if task.task_key != expected_key:
        raise InputIdentityError('Task key differs from its bound condition')
    for k, v in {**c, 'mof_id': task.mof_id, 'model_branch': task.model_branch}.items():
        if contract.get(k) != v:
            raise InputIdentityError('Task field differs from input contract: ' + k)
    ctx = static_context(task.mof_id, c, contract['cycles'], task.model_branch, task.descriptors)
    for k, v in contract['template_bindings'].items():
        if ctx.get(k) != v:
            raise InputIdentityError('Rendered descriptor/binding changed in task: ' + k)


def verify_task_inputs(config: CampaignConfig, task: Task) -> tuple[dict[str, Any], dict[str, bytes], dict[str, str]]:
    validate_task_record(task, require_identity=True)
    c = {k: getattr(task, k) for k in ('gas','temperature_K','pressure_bar','pressure_Pa','seed','replicate')}
    current, payload, paths = snapshot_inputs(config, task, c)
    if current != task.input_contract or canonical_sha256(current) != task.contract_sha256:
        raise InputIdentityError('Scientific input changed since task build: rebuild/replan a new task; do not retry with altered inputs')
    return current, payload, paths
