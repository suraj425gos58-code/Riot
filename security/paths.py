"""
Riot Secure Path Utilities
==========================

Central path-security boundary.

Used by:
- cloud storage
- build system
- asset system
- generated source
- project workspace
- backups
"""

from __future__ import annotations

import os

from pathlib import Path, PurePosixPath
from typing import Iterable


class UnsafePathError(ValueError):
    """Raised when a path violates security constraints."""


def safe_component(
    value: str,
    *,
    field: str = "path component",
    max_length: int = 128,
) -> str:

    normalized = str(
        value or ""
    ).strip()

    if not normalized:
        raise UnsafePathError(
            f"{field} cannot be empty"
        )

    if len(normalized) > max_length:
        raise UnsafePathError(
            f"{field} exceeds maximum length"
        )

    if (
        normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise UnsafePathError(
            f"unsafe {field}"
        )

    if os.path.altsep:
        if os.path.altsep in normalized:
            raise UnsafePathError(
                f"unsafe {field}"
            )

    return normalized


def safe_relative_path(
    value: str,
) -> str:

    raw = str(
        value or ""
    ).replace(
        "\\",
        "/",
    ).strip("/")

    if not raw:
        raise UnsafePathError(
            "relative path cannot be empty"
        )

    path = PurePosixPath(
        raw
    )

    if path.is_absolute():
        raise UnsafePathError(
            "absolute paths are forbidden"
        )

    if "\x00" in raw:
        raise UnsafePathError(
            "NUL byte in path"
        )

    if ".." in path.parts:
        raise UnsafePathError(
            "path traversal detected"
        )

    if any(
        part in {"", "."}
        for part in path.parts
    ):
        raise UnsafePathError(
            "invalid path component"
        )

    return path.as_posix()


def resolve_inside(
    root: str | Path,
    relative_path: str,
) -> Path:

    root_path = Path(
        root
    ).expanduser().resolve()

    safe_path = safe_relative_path(
        relative_path
    )

    candidate = (
        root_path
        / safe_path
    ).resolve()

    try:
        candidate.relative_to(
            root_path
        )
    except ValueError as exc:
        raise UnsafePathError(
            "resolved path escaped root"
        ) from exc

    return candidate


def assert_allowed_prefix(
    relative_path: str,
    allowed_prefixes: Iterable[str],
) -> str:

    safe_path = safe_relative_path(
        relative_path
    )

    prefixes = [
        safe_relative_path(
            prefix
        )
        for prefix in allowed_prefixes
    ]

    if not any(
        safe_path == prefix
        or safe_path.startswith(
            prefix + "/"
        )
        for prefix in prefixes
    ):
        raise UnsafePathError(
            f"path is outside allowed prefixes: "
            f"{safe_path}"
        )

    return safe_path


__all__ = [
    "UnsafePathError",
    "safe_component",
    "safe_relative_path",
    "resolve_inside",
    "assert_allowed_prefix",
]
