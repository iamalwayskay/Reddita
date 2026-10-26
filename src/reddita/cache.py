from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def open_private_cache(path: Path) -> int | Path:
    """Create and pin a private cache without following any path component."""
    absolute = path.absolute()
    components = absolute.parts[1:]
    if not components or any(component in {".", ".."} for component in components):
        raise ValueError("compatibility cache path is unsafe")
    if os.name == "nt":
        current = Path(absolute.anchor)
        for component in components:
            current /= component
            if current.exists() or _is_link_or_junction(current):
                if _is_link_or_junction(current) or not current.is_dir():
                    raise ValueError("compatibility cache path is not an owned real directory")
            else:
                current.mkdir(mode=0o700)
        return absolute
    descriptor = os.open(
        "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        for index, component in enumerate(components):
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                created = True
            except FileExistsError:
                pass
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            info = os.fstat(child)
            if (not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)):
                os.close(child)
                raise ValueError("compatibility cache path is not an owned real directory")
            if info.st_uid == os.geteuid() and info.st_mode & 0o022:
                os.close(child)
                raise ValueError("compatibility cache ancestry is writable by another user")
            os.close(descriptor)
            descriptor = child
            if created and info.st_mode & 0o077:
                raise ValueError("new compatibility cache directory is not private")
            if index == len(components) - 1:
                if info.st_uid != os.geteuid():
                    raise ValueError("compatibility cache must be owned by the current user")
                os.fchmod(descriptor, 0o700)
                final = os.fstat(descriptor)
                if final.st_mode & 0o077:
                    raise ValueError("compatibility cache must be private")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def record_rows(capture) -> list[tuple[str, dict]]:
    return [
        (record.record_type, json.loads(record.data_json))
        for record in capture.result.records
    ]
