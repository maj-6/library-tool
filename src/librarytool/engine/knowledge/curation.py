"""Pure materialization of canonical passage-curation overlays."""

from __future__ import annotations

from ..errors import ValidationError
from ._json import derived_revision
from .contracts import (
    CurationConflict,
    CurationMaterialization,
    PassageCurationOverlay,
    PassageSetView,
)


def materialize_curation(
    base: PassageSetView,
    overlay: PassageCurationOverlay,
) -> CurationMaterialization:
    """Apply v1 include/exclude decisions without mutating either input.

    A base-revision change is reported but does not discard operations whose
    stable passage ids still resolve.  Missing targets become explicit orphan
    conflicts.  A repository can use ``overlay.base_revision`` as its CAS
    precondition while this function remains persistence-neutral.
    """

    if not isinstance(base, PassageSetView):
        raise ValidationError(
            "base must be a PassageSetView",
            code="invalid_curation_input",
        )
    if not isinstance(overlay, PassageCurationOverlay):
        raise ValidationError(
            "overlay must be a PassageCurationOverlay",
            code="invalid_curation_input",
        )
    if base.item_id != overlay.item_id:
        raise ValidationError(
            "the curation overlay belongs to another item",
            code="curation_item_mismatch",
            details={"base_item_id": base.item_id, "overlay_item_id": overlay.item_id},
        )

    conflicts: list[CurationConflict] = []
    if base.base_revision != overlay.base_revision:
        conflicts.append(
            CurationConflict(
                operation_id="",
                passage_id="",
                code="base_revision_changed",
                details={
                    "expected": overlay.base_revision,
                    "actual": base.base_revision,
                },
            )
        )
    known = {passage.passage_id for passage in base.passages}
    excluded = set(base.excluded_passage_ids)
    for operation in overlay.operations:
        if operation.passage_id not in known:
            conflicts.append(
                CurationConflict(
                    operation_id=operation.operation_id,
                    passage_id=operation.passage_id,
                    code="orphaned_passage",
                )
            )
            continue
        if operation.action == "exclude":
            excluded.add(operation.passage_id)
        else:
            excluded.discard(operation.passage_id)

    aggregate_payload = {
        "base": base.as_dict(),
        "overlay": overlay.as_dict(),
        "excluded_passage_ids": sorted(excluded),
        "conflicts": [conflict.as_dict() for conflict in conflicts],
    }
    materialized = PassageSetView(
        item_id=base.item_id,
        representation_id=base.representation_id,
        layer_id=base.layer_id,
        source_revision=base.source_revision,
        base_revision=base.base_revision,
        curation_revision=overlay.revision,
        revision=derived_revision("pv", aggregate_payload),
        recipe=base.recipe,
        normalizer_id=base.normalizer_id,
        normalizer_version=base.normalizer_version,
        normalizer_revision=base.normalizer_revision,
        segmenter_id=base.segmenter_id,
        segmenter_version=base.segmenter_version,
        passages=base.passages,
        excluded_passage_ids=tuple(sorted(excluded)),
        metadata=base.metadata,
    )
    return CurationMaterialization(
        passage_set=materialized,
        overlay_revision=overlay.revision,
        conflicts=tuple(conflicts),
    )


__all__ = ["materialize_curation"]
