from __future__ import annotations

import gzip
import os
import shutil
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .hashing import sha256_file


def manifest_tree(root: Path, *, include_symlinks: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        if p.is_symlink() and not include_symlinks:
            rows.append({"relative_path": str(p.relative_to(root)), "role": "symlink", "target": str(p.resolve())})
            continue
        try:
            rows.append({
                "relative_path": str(p.relative_to(root)),
                "size_bytes": p.stat().st_size,
                "sha256": sha256_file(p),
                "role": _role(p),
            })
        except OSError as exc:
            rows.append({"relative_path": str(p.relative_to(root)), "role": "unreadable", "error": str(exc)})
    return rows


def _role(p: Path) -> str:
    name = p.name.lower()
    if name == "simulation.json": return "simulation_input"
    if name.endswith(".cif"): return "framework_cif"
    if "force_field" in name: return "force_field"
    if name.startswith("output_") and name.endswith(".txt"): return "raspa3_output_text"
    if name.startswith("output_") and name.endswith(".json"): return "raspa3_output_json"
    if name == "stdout.log": return "stdout"
    if name == "stderr.log": return "stderr"
    if "time" in name: return "resource_usage"
    return "other"


def gzip_verified(path: Path, *, delete_source: bool = False, level: int = 6) -> dict[str, Any]:
    source_sha = sha256_file(path)
    out = path.with_suffix(path.suffix + ".gz")
    tmp = out.with_suffix(out.suffix + ".tmp")
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=level) as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, out)
    import hashlib
    h = hashlib.sha256()
    with gzip.open(out, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    if h.hexdigest() != source_sha:
        out.unlink(missing_ok=True)
        raise IOError(f"Compression verification failed: {path}")
    if delete_source:
        path.unlink()
    return {"source_path": str(path), "source_sha256": source_sha, "compressed_path": str(out), "compressed_sha256": sha256_file(out), "verified": True}


def compress_completed_outputs(config, *, delete_raw: bool = False, yes: bool = False) -> dict[str, Any]:
    from .log_archive import compress_completed_outputs as service
    return service(config, delete_raw=delete_raw, yes=yes)
