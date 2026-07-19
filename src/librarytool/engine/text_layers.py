"""Application service for page-aligned derived text documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import PageKey
from .errors import ValidationError
from .ports import ReplicaPolicyPort, TextLayerRepositoryPort


class TextLayerService:
    """Compose and persist text layers without knowing their storage format."""

    def __init__(
        self,
        repository: TextLayerRepositoryPort,
        policies: ReplicaPolicyPort,
    ) -> None:
        self._repository = repository
        self._policies = policies

    def compose(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        layer: str = "text",
    ) -> str:
        normalized = self._normalize_layer(layer)
        return str(self._policies.compose_text(items, layer=normalized))

    @staticmethod
    def distribute(text: str, weights: Sequence[float]) -> list[str]:
        """Distribute page text at paragraph boundaries by region weight."""
        if not weights:
            return []
        paragraphs = [
            paragraph
            for paragraph in str(text or "").split("\n\n")
            if paragraph.strip()
        ]
        output: list[list[str]] = [[] for _ in weights]
        if not paragraphs:
            return ["" for _ in weights]
        numeric = [max(0.0, float(weight or 0)) for weight in weights]
        total_weight = sum(numeric) or 1.0
        total_chars = sum(len(paragraph) for paragraph in paragraphs) or 1
        index = 0
        consumed = 0
        for paragraph in paragraphs:
            output[min(index, len(output) - 1)].append(paragraph)
            consumed += len(paragraph)
            while (
                index < len(numeric) - 1
                and consumed / total_chars
                >= sum(numeric[: index + 1]) / total_weight
            ):
                index += 1
        return ["\n\n".join(group) for group in output]

    def merge_explicit(
        self,
        key: PageKey,
        document: str,
        text: str,
        *,
        bind_source: bool = False,
    ) -> str:
        self._validate_key(key)
        name = self._policies.sanitize_document_name(str(document or ""))
        if not name:
            raise ValidationError(
                "the text document name is invalid",
                code="invalid_document_name",
                details={"document": str(document or "")},
            )
        self._repository.merge_page(
            key.item_id, name, key.page, str(text or "")
        )
        if bind_source and key.source_id != "primary":
            self._repository.bind_document_source(
                key.item_id, name, key.source_id
            )
        return name

    def compile_region_page(
        self,
        key: PageKey,
        record: Mapping[str, Any],
        *,
        layer: str = "text",
        target: str = "",
    ) -> str:
        """Compose one canonical region record and merge it into a document."""
        normalized = self._normalize_layer(layer)
        if normalized == "norm":
            default = (
                "normalized.txt"
                if key.source_id == "primary"
                else f"normalized-{key.source_id}.txt"
            )
            document = target or default
        else:
            document = target or str(record.get("doc") or "compiled.txt")
        text = self.compose(record.get("items") or (), layer=normalized)
        return self.merge_explicit(
            key,
            document,
            text,
            bind_source=normalized == "norm",
        )

    @staticmethod
    def _normalize_layer(layer: str) -> str:
        value = str(layer or "text").strip().lower()
        if value in ("norm", "normalized"):
            return "norm"
        if value == "text":
            return value
        raise ValidationError(
            "the text layer must be text or norm",
            code="invalid_text_layer",
            details={"layer": value},
        )

    @staticmethod
    def _validate_key(key: PageKey) -> None:
        if not str(key.item_id or "").strip():
            raise ValidationError(
                "an item id is required",
                code="invalid_item_id",
                details={"item_id": key.item_id},
            )
        if not str(key.source_id or "").strip():
            raise ValidationError(
                "a source id is required",
                code="invalid_source_id",
                details={"source_id": key.source_id},
            )
        if not isinstance(key.page, int) or isinstance(key.page, bool) or key.page < 1:
            raise ValidationError(
                "page must be a positive integer",
                code="invalid_page",
                details={"page": key.page},
            )
