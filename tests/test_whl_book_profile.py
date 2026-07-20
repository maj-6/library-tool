"""WHL book rules remain reusable without importing the Flask host."""

from __future__ import annotations

import pytest

from librarytool.engine.errors import RepositoryError, ValidationError
from librarytool.engine.item_commands import (
    ItemDraft,
    ItemPatch,
    ItemRecordSnapshot,
    RepresentationDraft,
)
from librarytool.profiles.whl_book import WhlBookItemCommandPolicy


def _policy(category_ids=("plants", "medicine")):
    return WhlBookItemCommandPolicy(lambda: category_ids)


def _current(draft: ItemDraft) -> ItemRecordSnapshot:
    return ItemRecordSnapshot(
        item_id="book-1",
        revision="rev-1",
        kind=draft.kind,
        title=draft.title,
        metadata=draft.metadata,
        representations=draft.representations,
    )


def test_profile_accepts_valid_book_extensions_without_normalizing_candidate():
    calls = []
    policy = WhlBookItemCommandPolicy(
        lambda: calls.append("categories") or ("plants", "medicine")
    )
    candidate = ItemDraft(
        title="A New Herbal",
        metadata={
            "authors": "Ada Curator",
            "rights": "public-domain",
            "category_ids": ["plants"],
            "bundle": {
                "about": True,
                "annotations": False,
                "pages_text": True,
                "translations": ["fr", "pt-br"],
            },
            "custom_extension": {"score": 0.75},
        },
    )
    before = candidate.as_dict()

    assert policy.validate_create(candidate) is None

    assert candidate.as_dict() == before
    assert calls == ["categories"]


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        (ItemDraft(kind="article"), "unsupported_item_kind"),
        (
            ItemDraft(
                representations=(RepresentationDraft("primary"),),
            ),
            "representation_mutation_not_supported",
        ),
        (
            ItemDraft(metadata={"status": "draft"}),
            "managed_item_fields_not_writable",
        ),
    ],
)
def test_profile_rejects_non_book_representation_and_managed_create_fields(
    candidate,
    code,
):
    with pytest.raises(ValidationError) as caught:
        _policy().validate_create(candidate)

    assert caught.value.code == code


@pytest.mark.parametrize(
    ("metadata", "field", "reason"),
    [
        ({"authors": ["Ada"]}, "authors", "string_required"),
        ({"authors": " Ada "}, "authors", "outer_whitespace"),
        ({"rights": "unknown"}, "rights", "invalid_value"),
        ({"category_ids": "plants"}, "category_ids", "array_required"),
        (
            {"category_ids": ["plants", "plants"]},
            "category_ids",
            "invalid_value",
        ),
        ({"category_ids": ["too-long-category"]}, "category_ids", "invalid_value"),
        ({"bundle": []}, "bundle", "object_required"),
        ({"bundle": {"unknown": True}}, "bundle", "unknown_fields"),
        (
            {"bundle": {"about": "yes"}},
            "bundle.about",
            "boolean_required",
        ),
        (
            {"bundle": {"translations": ["FR"]}},
            "bundle.translations",
            "invalid_value",
        ),
        (
            {"bundle": {"translations": ["fr", "fr"]}},
            "bundle.translations",
            "invalid_value",
        ),
    ],
)
def test_profile_validates_known_whl_metadata_shapes(metadata, field, reason):
    with pytest.raises(ValidationError) as caught:
        _policy().validate_create(ItemDraft(metadata=metadata))

    assert caught.value.code == "invalid_item_metadata"
    assert caught.value.details["field"] == field
    assert caught.value.details["reason"] == reason


def test_profile_rejects_outer_title_whitespace_and_unknown_categories():
    with pytest.raises(ValidationError) as title_error:
        _policy().validate_create(ItemDraft(title=" Padded "))
    assert title_error.value.code == "invalid_item_metadata"
    assert title_error.value.details == {
        "field": "title",
        "reason": "outer_whitespace",
    }

    with pytest.raises(ValidationError) as category_error:
        _policy().validate_create(
            ItemDraft(metadata={"category_ids": ["unknown"]})
        )
    assert category_error.value.code == "invalid_item_metadata"
    assert category_error.value.details == {
        "field": "category_ids",
        "reason": "unknown_ids",
        "values": ["unknown"],
    }


def test_category_catalogue_failures_are_sanitized_repository_errors():
    def unavailable():
        raise RuntimeError("C:/private/taxonomy.json failed")

    with pytest.raises(RepositoryError) as caught:
        WhlBookItemCommandPolicy(unavailable).validate_create(
            ItemDraft(metadata={"category_ids": ["plants"]})
        )

    assert caught.value.code == "category_repository_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {"cause_type": "RuntimeError"}
    assert "private" not in str(caught.value.as_dict())


def test_category_catalogue_preserves_a_host_sanitized_repository_error():
    failure = RepositoryError(
        "the category catalogue is unavailable",
        code="category_repository_unavailable",
        retryable=True,
    )

    def unavailable():
        raise failure

    with pytest.raises(RepositoryError) as caught:
        WhlBookItemCommandPolicy(unavailable).validate_create(
            ItemDraft(metadata={"category_ids": ["plants"]})
        )

    assert caught.value is failure


def test_update_validates_applied_candidate_but_loads_taxonomy_only_when_touched():
    calls = []
    policy = WhlBookItemCommandPolicy(
        lambda: calls.append("categories") or ("plants",)
    )
    current = _current(
        ItemDraft(
            title="Legacy",
            metadata={
                # Existing outer whitespace remains transitional: only fields
                # explicitly written by the patch receive trim enforcement.
                "authors": " Legacy Curator ",
                "category_ids": ["legacy"],
                "rights": "cleared",
                "custom_extension": {"keep": True},
            },
        )
    )
    patch = ItemPatch(metadata_set={"notes": "Reviewed"})
    candidate = patch.apply(current)

    assert policy.validate_update(current, patch, candidate) is None
    assert calls == []

    category_patch = ItemPatch(metadata_set={"category_ids": ["plants"]})
    policy.validate_update(current, category_patch, category_patch.apply(current))
    assert calls == ["categories"]


def test_update_rejects_managed_removals_representation_changes_and_bad_writes():
    current = _current(ItemDraft(title="Legacy", metadata={"authors": "Ada"}))
    cases = (
        (
            ItemPatch(metadata_remove=("updated_at",)),
            "managed_item_fields_not_writable",
        ),
        (
            ItemPatch(representations=()),
            "representation_mutation_not_supported",
        ),
        (
            ItemPatch(title=" Padded "),
            "invalid_item_metadata",
        ),
        (
            ItemPatch(metadata_set={"authors": " Padded "}),
            "invalid_item_metadata",
        ),
    )

    for patch, code in cases:
        with pytest.raises(ValidationError) as caught:
            _policy().validate_update(current, patch, patch.apply(current))
        assert caught.value.code == code


def test_profile_requires_an_explicit_category_catalogue_port():
    with pytest.raises(TypeError, match="category_ids_for must be callable"):
        WhlBookItemCommandPolicy(None)
