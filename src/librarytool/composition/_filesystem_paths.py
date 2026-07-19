"""Shared containment checks for filesystem composition and host startup."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..engine.errors import RepositoryError


def _is_redirecting_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    if os.name == "nt" and os.path.lexists(path):
        try:
            attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
        except OSError:
            return False
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return bool(reparse and attributes & reparse)
    return False


def resolve_workspace_path(
    root: Path,
    configured: Path,
    *,
    artifact: str,
    directory: bool,
) -> Path:
    """Resolve one host path inside the workspace without following aliases."""

    if any(part in {".", ".."} for part in configured.parts):
        raise RepositoryError(
            "a filesystem engine path is ambiguous",
            code="unsafe_filesystem_engine_path",
            details={"artifact": artifact},
        )
    candidate = configured if configured.is_absolute() else root / configured
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise RepositoryError(
            "a filesystem engine path escapes the workspace",
            code="unsafe_filesystem_engine_path",
            details={"artifact": artifact},
        ) from exc
    if (
        not relative.parts
        or relative.parts[0].casefold() in {".engine", ".transactions"}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RepositoryError(
            "a filesystem engine path is reserved or ambiguous",
            code="unsafe_filesystem_engine_path",
            details={"artifact": artifact},
        )
    current = root
    for part in relative.parts:
        current /= part
        if _is_redirecting_path(current):
            raise RepositoryError(
                "a filesystem engine path crosses a redirect",
                code="unsafe_filesystem_engine_path",
                details={"artifact": artifact},
            )
    try:
        resolved = lexical.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RepositoryError(
            "a filesystem engine path cannot be resolved safely",
            code="unsafe_filesystem_engine_path",
            details={"artifact": artifact},
        ) from exc
    if directory and lexical.exists() and not lexical.is_dir():
        raise RepositoryError(
            "a filesystem engine directory is not a directory",
            code="unsafe_filesystem_engine_path",
            details={"artifact": artifact},
        )
    if not directory and (
        lexical.suffix.casefold() != ".json"
        or (lexical.exists() and not lexical.is_file())
    ):
        raise RepositoryError(
            "a filesystem engine JSON path is invalid",
            code="unsafe_filesystem_engine_path",
            details={"artifact": artifact},
        )
    return lexical


def workspace_paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either resolved artifact contains the other."""

    try:
        left.relative_to(right)
    except ValueError:
        pass
    else:
        return True
    try:
        right.relative_to(left)
    except ValueError:
        return False
    return True


__all__ = ["resolve_workspace_path", "workspace_paths_overlap"]
