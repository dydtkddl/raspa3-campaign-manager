from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256


class ConfigError(ValueError):
    pass


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_vars(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in env:
                raise ConfigError(f"Undefined template variable: {key}")
            return env[key]
        return _VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [expand_vars(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_vars(v, env) for k, v in value.items()}
    return value


def deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass(slots=True)
class CampaignConfig:
    root: Path
    path: Path
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return str(deep_get(self.data, "campaign.name", self.root.name))

    @property
    def engine_binary(self) -> Path:
        raw = str(deep_get(self.data, "engine.binary", "raspa3"))
        candidate = Path(os.path.expanduser(raw))
        if candidate.is_absolute():
            return candidate
        found = shutil.which(raw)
        return Path(found) if found else candidate

    @property
    def config_sha256(self) -> str:
        return canonical_sha256(self.data)

    def get(self, dotted: str, default: Any = None) -> Any:
        return deep_get(self.data, dotted, default)


def resolve_config_path(root_or_config: Path | str) -> Path:
    p = Path(root_or_config).expanduser().resolve()
    if p.is_dir():
        p = p / "campaign.toml"
    return p


def load_config(root_or_config: Path | str) -> CampaignConfig:
    path = resolve_config_path(root_or_config)
    if not path.is_file():
        raise ConfigError(f"Campaign config not found: {path}")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML: {path}: {exc}") from exc
    root = path.parent.resolve()
    validate_config(data, root)
    return CampaignConfig(root=root, path=path, data=data)


def validate_config(data: dict[str, Any], root: Path | None = None) -> list[str]:
    issues: list[str] = []
    if not isinstance(data.get("campaign"), dict):
        issues.append("Missing [campaign] section")
    if not isinstance(data.get("engine"), dict):
        issues.append("Missing [engine] section")
    if not isinstance(data.get("inputs"), dict):
        issues.append("Missing [inputs] section")
    matrix = data.get("matrix", {})
    conditions = matrix.get("conditions", []) if isinstance(matrix, dict) else []
    if not isinstance(conditions, list) or not conditions:
        issues.append("At least one [[matrix.conditions]] block is required")
    for idx, c in enumerate(conditions):
        if not isinstance(c, dict):
            issues.append(f"matrix.conditions[{idx}] is not a table")
            continue
        for key in ("gas", "temperatures_K", "pressures_bar", "seeds"):
            if key not in c:
                issues.append(f"matrix.conditions[{idx}] missing {key}")
        for p in c.get("pressures_bar", []):
            try:
                if float(p) <= 0:
                    issues.append(f"matrix.conditions[{idx}] pressure must be > 0: {p}")
            except (TypeError, ValueError):
                issues.append(f"matrix.conditions[{idx}] invalid pressure: {p}")
    workers = deep_get(data, "execution.workers", 1)
    try:
        if int(workers) < 1:
            issues.append("execution.workers must be >= 1")
    except (TypeError, ValueError):
        issues.append("execution.workers must be an integer")
    print_every = deep_get(data, "cycles.print_every", 1000)
    production = deep_get(data, "cycles.production", 0)
    try:
        if int(print_every) <= 0:
            issues.append("cycles.print_every must be > 0")
        if int(production) <= 0:
            issues.append("cycles.production must be > 0")
    except (TypeError, ValueError):
        issues.append("cycles values must be integers")
    if issues:
        raise ConfigError("; ".join(issues))
    return issues
