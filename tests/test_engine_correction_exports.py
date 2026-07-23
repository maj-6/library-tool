from __future__ import annotations

import librarytool.engine as engine
from librarytool.engine import (
    correction_transforms,
    corrections,
    raster_artifacts,
    spatial_annotations,
)


def test_correction_contracts_are_available_from_the_public_engine_package() -> None:
    expected = (
        set(corrections.__all__)
        | set(correction_transforms.__all__)
        | set(raster_artifacts.__all__)
        | set(spatial_annotations.__all__)
    )

    assert not sorted(name for name in expected if not hasattr(engine, name))


def test_correction_service_keys_are_stable_without_claiming_a_provider() -> None:
    assert engine.CORRECTION_SERVICE.token == "library.corrections.commands@1"
    assert (
        engine.CORRECTION_TRANSFORM_SERVICE.token
        == "library.corrections.transforms@1"
    )
    assert (
        engine.RASTER_ARTIFACT_QUERY_SERVICE.token
        == "library.raster-artifacts.query@1"
    )
    assert (
        engine.SPATIAL_ANNOTATION_QUERY_SERVICE.token
        == "library.spatial-annotations.query@1"
    )
    registry = engine.ServiceRegistry()
    assert registry.get(engine.CORRECTION_SERVICE) is None
    assert registry.get(engine.CORRECTION_TRANSFORM_SERVICE) is None
    assert registry.get(engine.RASTER_ARTIFACT_QUERY_SERVICE) is None
    assert registry.get(engine.SPATIAL_ANNOTATION_QUERY_SERVICE) is None
