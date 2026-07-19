"""Dependency-inversion ports for the headless engine slice."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any, ContextManager, Protocol

from .contracts import ItemDescriptor
from .translation_contracts import (
    TranslationAggregate,
    TranslationSourceSnapshot,
)


class ItemRepositoryPort(Protocol):
    def get(self, item_id: str) -> ItemDescriptor | None:
        """Return an item descriptor, or ``None`` when it does not exist."""


class ReplicaUnitOfWorkPort(Protocol):
    @property
    def workspace(self) -> MutableMapping[str, Any]:
        """The mutable Replica workspace held by this transaction."""

    def commit(self) -> None:
        """Durably persist the current workspace.

        A unit of work may commit more than once.  Proposal application relies
        on this to journal pending derived work before attempting that work.
        """


class ReplicaRepositoryPort(Protocol):
    def snapshot(self, item_id: str) -> Mapping[str, Any]:
        """Return an isolated, read-only-by-convention workspace snapshot."""

    def unit_of_work(self, item_id: str) -> ContextManager[ReplicaUnitOfWorkPort]:
        """Open a serialized mutable workspace transaction."""


class ReplicaPolicyPort(Protocol):
    """Existing domain/format behavior injected into the new engine.

    The transitional adapter may delegate these callbacks to today's policy
    modules.  Keeping the port here prevents the package from importing flat
    ``tools`` modules and lets those policies move independently later.
    """

    def content_revision(self, value: Any, prefix: str = "rr") -> str: ...

    def proposal_revision(self, proposal: Mapping[str, Any] | None) -> str: ...

    def duplicate_rids(self, items: Sequence[Mapping[str, Any]]) -> set[str]: ...

    def clean_rid(self, value: Any) -> str: ...

    def sanitize_region_items(
        self, items: Sequence[Mapping[str, Any]], *, source_type: str
    ) -> list[dict[str, Any]]: ...

    def sanitize_dims(self, dims: Mapping[str, Any]) -> dict[str, Any]: ...

    def sanitize_ext(self, ext: Mapping[str, Any]) -> dict[str, Any]: ...

    def sanitize_document_name(self, value: str) -> str: ...

    def normalize_language(self, value: str) -> str: ...

    def accept_region_proposal(
        self,
        current: Mapping[str, Any] | None,
        proposal: Mapping[str, Any],
    ) -> dict[str, Any] | None: ...

    def dismiss_region_proposal(
        self, current: Mapping[str, Any] | None
    ) -> dict[str, Any] | None: ...

    def compose_text(
        self, items: Sequence[Mapping[str, Any]], *, layer: str = "text"
    ) -> str: ...

    def propose_layout_families(
        self,
        pages: Mapping[str, Any],
        **options: Any,
    ) -> Mapping[str, Any]: ...


class TextLayerRepositoryPort(Protocol):
    def merge_page(
        self,
        item_id: str,
        document: str,
        page: int,
        text: str,
    ) -> None: ...

    def bind_document_source(
        self, item_id: str, document: str, source_id: str
    ) -> None: ...


class TranslationSourceSnapshotPort(Protocol):
    """Read authoritative source text while a repository lease is held."""

    def load_source(
        self, item_id: str, layer_id: str
    ) -> TranslationSourceSnapshot | None: ...


class TranslationReadSessionPort(TranslationSourceSnapshotPort, Protocol):
    """One coherent translation/source snapshot."""

    def list(self, item_id: str) -> Sequence[TranslationAggregate]: ...

    def load(
        self, item_id: str, translation_id: str
    ) -> TranslationAggregate | None: ...


class TranslationUnitOfWorkPort(TranslationReadSessionPort, Protocol):
    """Locked snapshot that can atomically compare and save an aggregate."""

    def compare_and_save(
        self,
        aggregate: TranslationAggregate,
        *,
        expected_document_revision: str,
        expected_source_revision: str,
    ) -> None:
        """Verify both revisions and save without releasing the lease."""


class TranslationRepositoryPort(Protocol):
    """Open coherent read and write sessions over translations and sources."""

    def snapshot(
        self, item_id: str
    ) -> ContextManager[TranslationReadSessionPort]: ...

    def unit_of_work(
        self, item_id: str
    ) -> ContextManager[TranslationUnitOfWorkPort]: ...


class TranslationPolicyPort(Protocol):
    """Small deterministic policy surface required by translation services."""

    def normalize_language(self, value: str) -> str: ...

    def revision(self, value: Any, prefix: str) -> str: ...


class JobHistoryRepositoryPort(Protocol):
    """Credential-free persistence for observable background-job history."""

    def load(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the last public snapshot, or an empty mapping."""

    def save(self, jobs: Mapping[str, Mapping[str, Any]]) -> None:
        """Atomically replace the public snapshot."""
