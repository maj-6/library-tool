"""Framework-neutral item, representation, and artifact queries.

This module is the read-only beginning of the Library Engine's catalogue
spine.  It deliberately knows nothing about Flask, filesystem paths, WHL
``build`` records, or a particular user interface.  A repository supplies
ordinary mapping-shaped snapshots; the service turns them into immutable,
revisioned views and derives machine-readable readiness facts from them.

Mutation commands belong in a later slice.  Keeping this first boundary
query-only lets current stores adopt it without changing their on-disk shape.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Iterable, Protocol

from .errors import NotFoundError, RepositoryError


JsonMapping = Mapping[str, Any]

_EMPTY_MAPPING: JsonMapping = MappingProxyType({})


def _freeze(
    value: Any,
    *,
    path: str = "$",
    active: set[int] | None = None,
) -> Any:
    """Detach and freeze strict JSON data from a repository boundary."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise RepositoryError(
            "an item query snapshot contains a non-finite number",
            code="invalid_item_snapshot",
            details={"path": path},
        )

    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise RepositoryError(
                "an item query snapshot contains a reference cycle",
                code="invalid_item_snapshot",
                details={"path": path},
            )
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise RepositoryError(
                        "item query object keys must be strings",
                        code="invalid_item_snapshot",
                        details={"path": path, "key_type": type(key).__name__},
                    )
                frozen[key] = _freeze(item, path=f"{path}.{key}", active=active)
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise RepositoryError(
                "an item query snapshot contains a reference cycle",
                code="invalid_item_snapshot",
                details={"path": path},
            )
        active.add(identity)
        try:
            return tuple(
                _freeze(item, path=f"{path}[{index}]", active=active)
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    raise RepositoryError(
        "an item query snapshot contains a non-JSON value",
        code="invalid_item_snapshot",
        details={"path": path, "value_type": type(value).__name__},
    )


def _thaw(value: Any) -> Any:
    """Return a JSON-serializable copy of a frozen view value."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            _thaw(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryError(
            "an item query snapshot is not JSON-compatible",
            code="invalid_item_snapshot",
            details={"reason": str(exc)},
        ) from exc


def _derived_revision(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _revision(raw: JsonMapping, prefix: str) -> str:
    explicit = str(raw.get("revision") or raw.get("updated_at") or "").strip()
    if explicit:
        return explicit
    value = {key: item for key, item in raw.items() if key != "revision"}
    return _derived_revision(prefix, value)


def _metadata(raw: JsonMapping, known: set[str]) -> JsonMapping:
    supplied = raw.get("metadata")
    if isinstance(supplied, Mapping):
        return _freeze(supplied)
    return _freeze({key: value for key, value in raw.items() if key not in known})


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _media_type(raw: JsonMapping, locator: str) -> str:
    supplied = str(raw.get("media_type") or raw.get("mime_type") or "").strip()
    if supplied:
        return supplied
    suffix = locator.lower().rsplit(".", 1)[-1] if "." in locator else ""
    return {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "txt": "text/plain",
        "md": "text/markdown",
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "mp4": "video/mp4",
    }.get(suffix, "application/octet-stream")


@dataclass(frozen=True, slots=True)
class RepresentationView:
    """One source or derivative representation associated with an item."""

    representation_id: str
    revision: str
    role: str = "source"
    media_type: str = "application/octet-stream"
    locator: str = ""
    label: str = ""
    canvas_count: int | None = None
    available: bool = True
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.representation_id,
            "revision": self.revision,
            "role": self.role,
            "media_type": self.media_type,
            "locator": self.locator,
            "label": self.label,
            "available": self.available,
            "metadata": _thaw(self.metadata),
        }
        if self.canvas_count is not None:
            value["canvas_count"] = self.canvas_count
        return value


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A concise reference to a derived or curated item artifact."""

    artifact_id: str
    revision: str
    kind: str
    name: str = ""
    layer: str = ""
    media_type: str = "application/octet-stream"
    source_representation_id: str = ""
    source_revision: str = ""
    stale: bool | None = None
    available: bool = True
    size: int | None = None
    provenance: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.artifact_id,
            "revision": self.revision,
            "kind": self.kind,
            "name": self.name,
            "layer": self.layer,
            "media_type": self.media_type,
            "source_representation_id": self.source_representation_id,
            "source_revision": self.source_revision,
            "stale": self.stale,
            "available": self.available,
            "provenance": _thaw(self.provenance),
            "metadata": _thaw(self.metadata),
        }
        if self.size is not None:
            value["size"] = self.size
        return value


@dataclass(frozen=True, slots=True)
class WorkbenchState:
    """Item prerequisites expressed without presentation language.

    ``readiness`` values are facts (``current``, ``stale``, ``untracked``,
    ``missing``, ``unavailable``), not instructions for a tab or button.
    ``available_commands`` describes item-state eligibility only.  A client
    must still intersect it with installed capabilities/provider health.
    """

    item_id: str
    revision: str
    readiness: JsonMapping
    issues: tuple[str, ...] = ()
    available_commands: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "revision": self.revision,
            "readiness": _thaw(self.readiness),
            "issues": list(self.issues),
            "available_commands": list(self.available_commands),
        }


@dataclass(frozen=True, slots=True)
class WorkbenchContext:
    """Immutable item facts supplied to independently installed policies."""

    item_id: str
    title: str
    metadata: JsonMapping
    representations: tuple[RepresentationView, ...]
    artifacts: tuple[ArtifactRef, ...]

    def artifact_readiness(self, kinds: frozenset[str]) -> str:
        """Summarize matching artifacts without hiding unavailable records."""

        rows = tuple(artifact for artifact in self.artifacts if artifact.kind in kinds)
        if not rows:
            return "missing"
        available = tuple(artifact for artifact in rows if artifact.available)
        if not available:
            return "unavailable"
        if any(artifact.stale is True for artifact in available):
            return "stale"
        if any(artifact.stale is None for artifact in available):
            return "untracked"
        return "current"


@dataclass(frozen=True, slots=True)
class WorkbenchContribution:
    """Facts and eligible commands contributed by one optional module."""

    readiness: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)
    issues: tuple[str, ...] = ()
    available_commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        frozen = _freeze(self.readiness, path="$.readiness")
        if not isinstance(frozen, Mapping):
            raise RepositoryError(
                "workbench policy readiness must be an object",
                code="invalid_workbench_policy",
            )
        object.__setattr__(self, "readiness", frozen)
        object.__setattr__(self, "issues", tuple(str(value) for value in self.issues))
        object.__setattr__(
            self,
            "available_commands",
            tuple(str(value) for value in self.available_commands),
        )


class WorkbenchPolicyPort(Protocol):
    """Optional module policy; no workbench names are hardcoded in items."""

    policy_id: str

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution: ...


@dataclass(frozen=True, slots=True)
class ItemView:
    """An immutable item snapshot suitable for any engine client."""

    item_id: str
    revision: str
    record_revision: str
    kind: str
    title: str
    metadata: JsonMapping
    representations: tuple[RepresentationView, ...]
    artifacts: tuple[ArtifactRef, ...]
    workbench_state: WorkbenchState

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "revision": self.revision,
            "record_revision": self.record_revision,
            "kind": self.kind,
            "title": self.title,
            "metadata": _thaw(self.metadata),
            "representations": [item.as_dict() for item in self.representations],
            "artifacts": [item.as_dict() for item in self.artifacts],
            "workbench_state": self.workbench_state.as_dict(),
        }


class ItemQueryRepositoryPort(Protocol):
    """Read-only snapshots consumed by :class:`ItemQueryService`."""

    def list_records(self) -> Sequence[JsonMapping]: ...

    def get_record(self, item_id: str) -> JsonMapping | None: ...

    def list_representation_records(
        self, item_id: str, item_record: JsonMapping | None = None
    ) -> Sequence[JsonMapping]: ...

    def list_artifact_records(
        self, item_id: str, item_record: JsonMapping | None = None
    ) -> Sequence[JsonMapping]: ...


class ItemQueryService:
    """List and inspect items without exposing their concrete store."""

    def __init__(
        self,
        repository: ItemQueryRepositoryPort,
        *,
        policies: Iterable[WorkbenchPolicyPort] = (),
    ) -> None:
        self._repository = repository
        if isinstance(policies, (str, bytes)):
            raise TypeError("policies must contain workbench policy objects")
        self._policies = tuple(policies)
        ids: list[str] = []
        for policy in self._policies:
            policy_id = str(getattr(policy, "policy_id", "") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", policy_id) or not callable(
                getattr(policy, "contribute", None)
            ):
                raise TypeError(
                    "each workbench policy needs a policy_id and contribute method"
                )
            ids.append(policy_id)
        duplicate = sorted({value for value in ids if ids.count(value) > 1})
        if duplicate:
            raise TypeError(f"duplicate workbench policy ids: {', '.join(duplicate)}")

    @property
    def policies(self) -> tuple[WorkbenchPolicyPort, ...]:
        """Return the immutable policy set selected by engine composition."""

        return self._policies

    def with_policies(
        self,
        policies: Iterable[WorkbenchPolicyPort],
    ) -> ItemQueryService:
        """Clone this query service over the same repository and new policies.

        Module resolution happens after concrete services have been offered to
        the runtime builder.  Cloning lets the builder attach policies owned by
        active modules without mutating the seed service or its repository.
        """

        return ItemQueryService(self._repository, policies=policies)

    def list_items(self) -> tuple[ItemView, ...]:
        records = self._records(self._repository.list_records(), "items")
        views = [self._view(record) for record in records]
        ids = [view.item_id for view in views]
        duplicate = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
        if duplicate:
            raise RepositoryError(
                "the item repository returned duplicate identities",
                code="duplicate_item_identity",
                details={"item_ids": duplicate},
            )
        return tuple(
            sorted(views, key=lambda view: (view.title.casefold(), view.item_id))
        )

    def get_item(self, item_id: str) -> ItemView:
        value = str(item_id or "").strip()
        record = self._repository.get_record(value) if value else None
        if record is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": value},
            )
        if not isinstance(record, Mapping):
            raise RepositoryError(
                "the item repository returned an invalid record",
                code="invalid_item_record",
                details={"item_id": value},
            )
        return self._view(record, expected_id=value)

    def list_representations(self, item_id: str) -> tuple[RepresentationView, ...]:
        return self.get_item(item_id).representations

    def list_artifacts(self, item_id: str) -> tuple[ArtifactRef, ...]:
        return self.get_item(item_id).artifacts

    def readiness(self, item_id: str) -> WorkbenchState:
        return self.get_item(item_id).workbench_state

    def _view(self, record: JsonMapping, *, expected_id: str = "") -> ItemView:
        item_id = str(record.get("item_id") or record.get("id") or expected_id).strip()
        if not item_id or (expected_id and item_id != expected_id):
            raise RepositoryError(
                "the item repository returned an inconsistent identity",
                code="invalid_item_identity",
                details={"expected": expected_id, "actual": item_id},
            )

        representation_records = self._records(
            self._repository.list_representation_records(item_id, record),
            "representations",
            item_id=item_id,
        )
        artifact_records = self._records(
            self._repository.list_artifact_records(item_id, record),
            "artifacts",
            item_id=item_id,
        )
        representations = tuple(
            sorted(
                (self._representation(raw) for raw in representation_records),
                key=lambda value: (
                    value.role != "primary",
                    value.label.casefold(),
                    value.representation_id,
                ),
            )
        )
        artifacts = tuple(
            sorted(
                (self._artifact(raw) for raw in artifact_records),
                key=lambda value: (
                    value.kind,
                    value.layer,
                    value.name.casefold(),
                    value.artifact_id,
                ),
            )
        )
        self._unique_ids(
            (item.representation_id for item in representations),
            "duplicate_representation_identity",
            item_id,
        )
        self._unique_ids(
            (item.artifact_id for item in artifacts),
            "duplicate_artifact_identity",
            item_id,
        )
        artifacts = self._reconcile_artifact_sources(artifacts, representations)

        known = {
            "artifacts",
            "id",
            "item_id",
            "kind",
            "metadata",
            "representations",
            "revision",
            "title",
            "updated_at",
        }
        metadata = _metadata(record, known)
        record_revision = _revision(record, "ir")
        title = str(record.get("title") or "").strip()
        kind = str(record.get("kind") or "book").strip() or "item"
        state = self._workbench_state(
            item_id,
            record_revision,
            title,
            metadata,
            representations,
            artifacts,
        )
        view_revision = _derived_revision(
            "iv",
            {
                "record_revision": record_revision,
                "kind": kind,
                "title": title,
                "metadata": metadata,
                "representations": [value.as_dict() for value in representations],
                "artifacts": [value.as_dict() for value in artifacts],
                "workbench_state": state.as_dict(),
            },
        )
        return ItemView(
            item_id=item_id,
            revision=view_revision,
            record_revision=record_revision,
            kind=kind,
            title=title,
            metadata=metadata,
            representations=representations,
            artifacts=artifacts,
            workbench_state=state,
        )

    @staticmethod
    def _records(
        value: Any, name: str, *, item_id: str = ""
    ) -> tuple[JsonMapping, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise RepositoryError(
                f"the item repository returned invalid {name}",
                code="invalid_item_snapshot",
                details={"item_id": item_id, "section": name},
            )
        out = tuple(value)
        if any(not isinstance(record, Mapping) for record in out):
            raise RepositoryError(
                f"the item repository returned invalid {name}",
                code="invalid_item_snapshot",
                details={"item_id": item_id, "section": name},
            )
        return out

    @staticmethod
    def _unique_ids(values: Any, code: str, item_id: str) -> None:
        rows = list(values)
        duplicate = sorted({value for value in rows if rows.count(value) > 1})
        if duplicate:
            raise RepositoryError(
                "the item repository returned duplicate identities",
                code=code,
                details={"item_id": item_id, "ids": duplicate},
            )

    @staticmethod
    def _representation(raw: JsonMapping) -> RepresentationView:
        locator = str(raw.get("locator") or raw.get("path") or raw.get("uri") or "")
        role = str(raw.get("role") or "source").strip().lower() or "source"
        representation_id = str(
            raw.get("representation_id") or raw.get("source_id") or raw.get("id") or ""
        ).strip()
        if not representation_id:
            representation_id = (
                "rep-"
                + hashlib.sha256(f"{role}\n{locator}".encode("utf-8")).hexdigest()[:16]
            )
        available = raw.get("available")
        if not isinstance(available, bool):
            available = bool(locator)
        known = {
            "available",
            "canvas_count",
            "id",
            "label",
            "locator",
            "media_type",
            "metadata",
            "mime_type",
            "pages",
            "path",
            "representation_id",
            "revision",
            "role",
            "source_id",
            "updated_at",
            "uri",
        }
        return RepresentationView(
            representation_id=representation_id,
            revision=_revision(raw, "rv"),
            role=role,
            media_type=_media_type(raw, locator),
            locator=locator,
            label=str(raw.get("label") or "").strip(),
            canvas_count=_positive_int(
                raw.get("canvas_count") if "canvas_count" in raw else raw.get("pages")
            ),
            available=available,
            metadata=_metadata(raw, known),
        )

    @staticmethod
    def _artifact(raw: JsonMapping) -> ArtifactRef:
        name = str(raw.get("name") or raw.get("path") or "").strip()
        kind = str(raw.get("kind") or "artifact").strip().lower() or "artifact"
        layer = str(raw.get("layer") or raw.get("lang") or "").strip().lower()
        source = str(
            raw.get("source_representation_id")
            or raw.get("source_id")
            or raw.get("src")
            or ""
        ).strip()
        artifact_id = str(raw.get("artifact_id") or raw.get("id") or "").strip()
        if not artifact_id:
            artifact_id = (
                "art-"
                + hashlib.sha256(
                    f"{kind}\n{layer}\n{name}\n{source}".encode("utf-8")
                ).hexdigest()[:16]
            )
        stale = raw.get("stale") if isinstance(raw.get("stale"), bool) else None
        available = raw.get("available")
        if not isinstance(available, bool):
            available = bool(raw.get("exists", True))
        provenance = raw.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = raw.get("produced_by")
        if not isinstance(provenance, Mapping):
            provenance = {}
        known = {
            "artifact_id",
            "available",
            "exists",
            "id",
            "kind",
            "lang",
            "layer",
            "media_type",
            "metadata",
            "mime_type",
            "name",
            "path",
            "produced_by",
            "provenance",
            "revision",
            "size",
            "source_id",
            "source_representation_id",
            "source_revision",
            "src",
            "stale",
            "updated_at",
        }
        return ArtifactRef(
            artifact_id=artifact_id,
            revision=_revision(raw, "av"),
            kind=kind,
            name=name,
            layer=layer,
            media_type=_media_type(raw, name),
            source_representation_id=source,
            source_revision=str(raw.get("source_revision") or "").strip(),
            stale=stale,
            available=available,
            size=_positive_int(raw.get("size")),
            provenance=_freeze(provenance),
            metadata=_metadata(raw, known),
        )

    @staticmethod
    def _reconcile_artifact_sources(
        artifacts: tuple[ArtifactRef, ...],
        representations: tuple[RepresentationView, ...],
    ) -> tuple[ArtifactRef, ...]:
        """Derive freshness from a recorded source identity and revision.

        Repository ``stale`` flags remain useful for artifacts whose producer
        tracks dependencies outside the representation spine.  Once an
        artifact names a representation, however, the engine must not report
        it as current unless its recorded source revision matches the current
        representation snapshot.  Missing provenance is therefore
        ``untracked`` rather than an optimistic current state.
        """

        sources = {
            representation.representation_id: representation
            for representation in representations
        }
        reconciled: list[ArtifactRef] = []
        for artifact in artifacts:
            source_id = artifact.source_representation_id
            if not source_id or artifact.stale is True:
                reconciled.append(artifact)
                continue

            source = sources.get(source_id)
            if source is None:
                stale: bool | None = True
            elif not artifact.source_revision:
                stale = None
            else:
                stale = artifact.source_revision != source.revision
            reconciled.append(replace(artifact, stale=stale))
        return tuple(reconciled)

    def _workbench_state(
        self,
        item_id: str,
        record_revision: str,
        title: str,
        metadata: JsonMapping,
        representations: tuple[RepresentationView, ...],
        artifacts: tuple[ArtifactRef, ...],
    ) -> WorkbenchState:
        usable_representations = tuple(
            item for item in representations if item.available
        )
        readiness: dict[str, Any] = {
            "record": "current" if title else "incomplete",
            "source": (
                "current"
                if usable_representations
                else "unavailable"
                if representations
                else "missing"
            ),
        }
        issues: list[str] = []
        if not title:
            issues.append("record.title_missing")
        if readiness["source"] == "missing":
            issues.append("representation.missing")
        elif readiness["source"] == "unavailable":
            issues.append("representation.unavailable")
        commands = {"item.metadata.edit", "representation.attach"}
        if artifacts:
            commands.add("artifact.inspect")
        if representations:
            commands.add("representation.inspect")

        context = WorkbenchContext(
            item_id=item_id,
            title=title,
            metadata=metadata,
            representations=representations,
            artifacts=artifacts,
        )
        for policy in self._policies:
            try:
                contribution = policy.contribute(context)
                if not isinstance(contribution, WorkbenchContribution):
                    raise TypeError("invalid workbench contribution")
                overlap = sorted(set(readiness) & set(contribution.readiness))
                if overlap:
                    raise ValueError(
                        f"duplicate workbench readiness facts: {', '.join(overlap)}"
                    )
            except Exception:
                # Optional modules are failure-isolated. Their diagnostics can
                # be recorded by the composition root, while every client gets
                # a usable core item and a stable machine-readable degradation.
                issues.append(f"module.{policy.policy_id}.unavailable")
                continue
            readiness.update(contribution.readiness)
            issues.extend(contribution.issues)
            commands.update(contribution.available_commands)

        normalized_issues = tuple(dict.fromkeys(issues))
        normalized_commands = tuple(sorted(commands))
        aggregate = {
            "record": record_revision,
            "representations": [item.revision for item in representations],
            "artifacts": [item.revision for item in artifacts],
            "readiness": readiness,
            "issues": normalized_issues,
            "available_commands": normalized_commands,
            # Rights and similar policy inputs live in metadata. Including the
            # whole frozen mapping ensures the state revision changes even
            # when an adapter supplies an explicit item revision incorrectly.
            "metadata": metadata,
        }
        return WorkbenchState(
            item_id=item_id,
            revision=_derived_revision("ws", aggregate),
            readiness=_freeze(readiness),
            issues=normalized_issues,
            available_commands=normalized_commands,
        )


__all__ = [
    "ArtifactRef",
    "ItemQueryRepositoryPort",
    "ItemQueryService",
    "ItemView",
    "RepresentationView",
    "WorkbenchState",
    "WorkbenchContext",
    "WorkbenchContribution",
    "WorkbenchPolicyPort",
]
