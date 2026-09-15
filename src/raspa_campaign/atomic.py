from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def atomic_write_bytes(path: Path | str, data: bytes, mode: int | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
        try:
            dir_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (AttributeError, OSError):
            pass
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8", mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(path: Path | str, value: Any, *, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True) + "\n")


def append_jsonl(path: Path | str, records: Iterable[dict[str, Any]]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
    fd = os.open(target, flags, 0o644)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        pass
    return count


def safe_mkdir_claim(path: Path | str) -> bool:
    try:
        Path(path).mkdir(parents=False, exist_ok=False)
        return True
    except FileExistsError:
        return False
