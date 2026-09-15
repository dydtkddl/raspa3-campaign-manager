"""P03 filesystem boundaries for campaign tasks and explicit input files.

Reject static symlink escapes and non-regular files. This is not a sandbox
against another process with the same OS user continuously replacing parents.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from .config import ConfigError


class PathSafetyError(ConfigError):
    pass


def validate_task_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r'task_[0-9a-f]{20}', value) is None:
        raise PathSafetyError('Invalid task ID: expected task_ followed by 20 lowercase hex digits')
    return value


def safe_name(value: str, label: str = 'name') -> str:
    # MOF IDs may contain spaces or Unicode, but never path/code delimiters.
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 200
            or value in {'.', '..'} or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in '/\\:$"\'`<>|?*')):
        raise PathSafetyError(f'{label}: a bounded filename without path separators/control characters is required')
    return value


def no_symlinks(path: Path) -> Path:
    path = Path(path).expanduser()
    if '..' in path.parts:
        raise PathSafetyError('Parent traversal is not allowed in managed paths')
    path = path.absolute()
    for part in (*reversed(path.parents), path):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise PathSafetyError('Symlink component in managed path')
        if part != path and not stat.S_ISDIR(mode):
            raise PathSafetyError('Non-directory parent in managed path')
    return path


def within(root: Path, relative: str) -> Path:
    p = PurePosixPath(relative)
    if (not isinstance(relative, str) or not relative or p.is_absolute()
            or '..' in p.parts or '\\' in relative or str(p) != relative
            or ':' in relative or any(ord(c) < 32 or ord(c) == 127 for c in relative)):
        raise PathSafetyError('Invalid campaign-relative path')
    base = no_symlinks(root)
    result = no_symlinks(base.joinpath(*p.parts))
    if not result.is_relative_to(base):
        raise PathSafetyError('Path escapes campaign root')
    return result


def task_directory(root: Path, task_id: str) -> Path:
    return within(root, 'tasks/' + validate_task_id(task_id))


def attempt_directory(root: Path, task_id: str, number: int) -> Path:
    if type(number) is not int or number < 1 or number > 99999999:
        raise PathSafetyError('Attempt number must be a positive bounded integer')
    return within(root, f'tasks/{validate_task_id(task_id)}/attempts/attempt_{number:04d}')


def read_regular(path: Path, *, maximum_bytes: int | None = None, single_link: bool = False) -> bytes:
    path = no_symlinks(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PathSafetyError('Only regular files may be read')
        if single_link and before.st_nlink != 1:
            raise PathSafetyError('Hard-linked managed input is refused')
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise PathSafetyError('Managed file exceeds its configured size bound')
        with os.fdopen(fd, 'rb', closefd=False) as fh:
            raw = fh.read() if maximum_bytes is None else fh.read(maximum_bytes + 1)
        after = os.fstat(fd)
        current = path.lstat()
        key = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if (key(before) != key(after) or key(after) != key(current)
                or len(raw) != after.st_size or (maximum_bytes is not None and len(raw) > maximum_bytes)):
            raise PathSafetyError('File changed while being read')
        no_symlinks(path)
        return raw
    finally:
        os.close(fd)


def read_json_file(path: Path) -> Any:
    return json.loads(read_regular(path, maximum_bytes=64 * 1024 * 1024).decode('utf-8'))


def tree_files(root: Path) -> list[Path]:
    root = no_symlinks(root)
    if not root.is_dir():
        raise FileNotFoundError('Input directory does not exist: ' + str(root))
    files = []
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = no_symlinks(Path(base) / name)
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise PathSafetyError('Non-regular entry in input directory')
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())
