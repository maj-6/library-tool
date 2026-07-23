"""Concrete lib/3 materialization for capture archive source snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..engine.capture_archives import (
    CAPTURE_LIB_FORMAT_VERSION,
    CaptureArchiveSource,
)
from ..engine.errors import RepositoryError, ValidationError


class Lib3CaptureArchiveMaterializer:
    """Seal engine capture sources through the standalone ``libformat`` core.

    ``libformat`` still lives in the transitional ``tools`` module tree.  The
    module is injected here so the installable engine package does not gain an
    import-time dependency on that compatibility layout.
    """

    def __init__(
        self,
        format_module: Any,
        *,
        generator: str = "library-tool/dev",
    ) -> None:
        required = (
            "LibArtifact",
            "LibDocument",
            "LibRepresentation",
            "read_lib",
            "seal_lib",
            "validate",
        )
        missing = [name for name in required if not hasattr(format_module, name)]
        if missing:
            raise TypeError(
                "format_module does not expose the lib/3 API: " + ", ".join(missing)
            )
        if (
            not isinstance(generator, str)
            or not generator
            or len(generator) > 128
            or any(ord(character) < 32 for character in generator)
        ):
            raise TypeError("generator must be a bounded string")
        self._format = format_module
        self._generator = generator

    def materialize(
        self,
        source: CaptureArchiveSource,
        *,
        book_id: str,
    ) -> bytes:
        if not isinstance(source, CaptureArchiveSource):
            raise TypeError("source must be a CaptureArchiveSource")
        manifest = source.manifest_copy()
        representation_values = manifest.pop("representations")
        artifact_values = manifest.pop("artifacts")
        if not isinstance(representation_values, list) or not isinstance(
            artifact_values, list
        ):
            raise ValidationError(
                "capture source graph arrays are invalid",
                code="invalid_capture_archive_command",
            )
        manifest["format_version"] = CAPTURE_LIB_FORMAT_VERSION
        manifest["book_id"] = book_id
        instructions = manifest.get("instructions")
        instructions_book = (
            str(instructions.get("book") or "")
            if isinstance(instructions, Mapping)
            else ""
        )
        try:
            document = self._format.LibDocument(
                format=(3, 0),
                book=manifest,
                pages=[],
                representations=[
                    self._format.LibRepresentation.from_dict(value)
                    for value in representation_values
                ],
                artifacts=[
                    self._format.LibArtifact.from_dict(value)
                    for value in artifact_values
                ],
                resources=dict(source.resources),
            )
            payload = self._format.seal_lib(
                document,
                generator=self._generator,
                book_id=book_id,
                instructions_book=instructions_book,
            )
            opened = self._format.read_lib(payload)
            issues = self._format.validate(opened)
        except Exception as exc:
            lib_error = getattr(self._format, "LibError", ())
            if lib_error and isinstance(exc, lib_error):
                raise ValidationError(
                    "the capture source cannot be sealed as lib/3",
                    code=str(
                        getattr(exc, "code", "")
                        or "invalid_capture_archive_materialization"
                    ),
                    details=dict(getattr(exc, "details", {}) or {}),
                ) from exc
            if isinstance(exc, (ValidationError, RepositoryError)):
                raise
            raise RepositoryError(
                "the lib/3 materializer failed",
                code="capture_archive_materialization_failed",
                details={"cause": type(exc).__name__},
                retryable=True,
            ) from exc
        errors = [
            issue
            for issue in issues
            if str(getattr(issue, "level", "")).casefold() == "error"
        ]
        if errors:
            raise ValidationError(
                "the materialized capture archive is invalid",
                code="invalid_capture_archive_materialization",
                details={
                    "issues": [
                        issue.as_dict()
                        if callable(getattr(issue, "as_dict", None))
                        else {
                            "level": str(getattr(issue, "level", "")),
                            "loc": str(getattr(issue, "loc", "")),
                            "msg": str(getattr(issue, "msg", "")),
                        }
                        for issue in errors
                    ]
                },
            )
        if (
            opened.format_version != CAPTURE_LIB_FORMAT_VERSION
            or opened.book_id != book_id
        ):
            raise RepositoryError(
                "the lib/3 materializer returned another archive identity",
                code="invalid_capture_archive_materialization",
            )
        return bytes(payload)


__all__ = ["Lib3CaptureArchiveMaterializer"]
