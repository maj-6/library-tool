from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from librarytool.processing.operations import (
    MAX_OPERATIONS_PER_RECIPE,
    PROCESSING_ALGORITHMS,
    ChannelGainOperation,
    ContrastOperation,
    GammaOperation,
    KernelSharpenOperation,
    ProcessingOperation,
    ProcessingOperationError,
    UnsharpMaskOperation,
    WhiteBalanceOperation,
    apply_operations,
    operation_from_dict,
    operations_from_list,
    validate_processing_operation,
)
from librarytool.processing.raster import apply_perspective_transform


FULL_FRAME = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def _sample(width: int = 32, height: int = 32) -> Image.Image:
    image = Image.new("RGB", (width, height))
    for x in range(width):
        for y in range(height):
            image.putpixel((x, y), ((x * 8) % 256, (y * 8) % 256, 120))
    return image


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _every_operation() -> tuple:
    return (
        UnsharpMaskOperation(radius_tenths=15, amount_percent=180, threshold=3),
        KernelSharpenOperation(strength_percent=40),
        GammaOperation(gamma_hundredths=120),
        ContrastOperation(contrast_percent=25),
        ChannelGainOperation(red_percent=110, green_percent=100, blue_percent=95),
        WhiteBalanceOperation(mode="gray_world", strength_percent=100),
        WhiteBalanceOperation(
            mode="manual", strength_percent=80, temperature=30, tint=-10
        ),
    )


def test_every_published_algorithm_has_an_operation() -> None:
    covered = {operation.algorithm for operation in _every_operation()}
    assert covered == set(PROCESSING_ALGORITHMS)


@pytest.mark.parametrize("operation", _every_operation(), ids=lambda o: o.algorithm)
def test_algorithm_discriminators_are_not_caller_overridable(operation) -> None:
    operation_type = type(operation)

    with pytest.raises(TypeError, match="algorithm"):
        operation_type(algorithm="another-operation-v1")


def test_the_base_operation_is_not_a_canonical_recipe() -> None:
    with pytest.raises(ProcessingOperationError, match="canonical supported"):
        validate_processing_operation(ProcessingOperation())


@pytest.mark.parametrize("operation", _every_operation(), ids=lambda o: o.algorithm)
def test_operations_round_trip_through_their_canonical_wire_form(operation) -> None:
    payload = operation.as_dict()

    assert payload["schema"] == "org.whl.raster.processing-operation"
    assert payload["version"] == 1
    assert payload["rule"]
    assert operation_from_dict(payload) == operation
    # Every wire parameter is an integer or the mode enum: a float would have to
    # survive Python and JavaScript canonical JSON identically, and they differ
    # on exponent formatting.
    for name, value in payload.items():
        if name in {"schema", "algorithm", "rule", "mode"}:
            assert isinstance(value, str)
        else:
            assert isinstance(value, int) and not isinstance(value, bool)


@pytest.mark.parametrize("operation", _every_operation(), ids=lambda o: o.algorithm)
def test_operations_render_deterministically(operation) -> None:
    sample = _sample()

    assert operation.apply(sample).tobytes() == operation.apply(sample).tobytes()


def test_neutral_parameters_leave_the_image_untouched() -> None:
    sample = _sample()

    for neutral in (
        GammaOperation(gamma_hundredths=100),
        ContrastOperation(contrast_percent=0),
        ChannelGainOperation(),
        KernelSharpenOperation(strength_percent=0),
        WhiteBalanceOperation(mode="manual", strength_percent=0),
    ):
        assert neutral.apply(sample).tobytes() == sample.tobytes(), neutral.algorithm


def test_gamma_lifts_midtones_and_contrast_widens_the_range() -> None:
    midtone = Image.new("RGB", (4, 4), (100, 100, 100))

    lifted = GammaOperation(gamma_hundredths=220).apply(midtone).getpixel((0, 0))
    assert lifted[0] > 100

    stretched = ContrastOperation(contrast_percent=100).apply(midtone).getpixel((0, 0))
    flattened = ContrastOperation(contrast_percent=-100).apply(midtone).getpixel((0, 0))
    assert stretched[0] < 100 < flattened[0] + 1
    assert flattened == (128, 128, 128)


def test_gray_world_white_balance_neutralizes_a_cast() -> None:
    cast = Image.new("RGB", (8, 8), (200, 150, 100))

    balanced = WhiteBalanceOperation(
        mode="gray_world",
        strength_percent=100,
    ).apply(cast).getpixel((0, 0))

    assert max(balanced) - min(balanced) < max(200, 150, 100) - min(200, 150, 100)


def test_manual_white_balance_warms_and_cools_predictably() -> None:
    neutral = Image.new("RGB", (4, 4), (128, 128, 128))

    warm = WhiteBalanceOperation(
        mode="manual", strength_percent=100, temperature=100
    ).apply(neutral).getpixel((0, 0))
    cool = WhiteBalanceOperation(
        mode="manual", strength_percent=100, temperature=-100
    ).apply(neutral).getpixel((0, 0))

    assert warm[0] > 128 > warm[2]
    assert cool[2] > 128 > cool[0]


def test_grayscale_input_survives_a_neutral_tone_operation() -> None:
    grayscale = Image.new("L", (8, 8), 90)

    assert GammaOperation(gamma_hundredths=140).apply(grayscale).mode == "L"
    # White balance is inherently a colour operation, so it widens instead.
    assert WhiteBalanceOperation(
        mode="manual", strength_percent=50, temperature=20
    ).apply(grayscale).mode == "RGB"


def test_pipeline_order_is_significant_and_never_reordered() -> None:
    sample = _sample()
    gamma = GammaOperation(gamma_hundredths=180)
    contrast = ContrastOperation(contrast_percent=80)

    assert apply_operations(sample, [gamma, contrast]).tobytes() != (
        apply_operations(sample, [contrast, gamma]).tobytes()
    )


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: GammaOperation(gamma_hundredths=5), "gamma_hundredths"),
        (lambda: GammaOperation(gamma_hundredths=1001), "gamma_hundredths"),
        (lambda: ContrastOperation(contrast_percent=101), "contrast_percent"),
        (lambda: UnsharpMaskOperation(radius_tenths=0), "radius_tenths"),
        (lambda: UnsharpMaskOperation(threshold=256), "threshold"),
        (lambda: ChannelGainOperation(red_percent=401), "red_percent"),
        (lambda: KernelSharpenOperation(strength_percent=-1), "strength_percent"),
        (lambda: GammaOperation(gamma_hundredths=True), "gamma_hundredths"),
        (lambda: WhiteBalanceOperation(mode="auto"), "gray_world or manual"),
        (
            lambda: WhiteBalanceOperation(mode="gray_world", temperature=10),
            "does not take temperature",
        ),
    ],
)
def test_out_of_range_parameters_are_rejected(factory, message: str) -> None:
    with pytest.raises(ProcessingOperationError, match=message):
        factory()


def test_the_shared_authoring_fixture_is_engine_canonical() -> None:
    """Pin the fixture the corrections UI builds its wire documents against.

    corrections_image_adjust_tool_behavior.test.js asserts the JavaScript
    authoring helper reproduces every ``canonical`` entry byte-for-byte; this
    side proves those entries are exactly what ``operation_from_dict`` accepts
    and what constructing the operation from the ``authoring`` parameters
    serializes to.  Together the two suites guarantee a UI-authored recipe is
    never rejected by the content-addressed transform command.
    """

    fixture = json.loads(
        (Path(__file__).with_name("fixtures") / "processing_operations_canonical.json")
        .read_text(encoding="utf-8")
    )
    assert fixture["schema"] == "librarytool.processing-operations-canonical/1"
    entries = fixture["operations"]
    covered = {entry["authoring"]["algorithm"] for entry in entries}
    assert covered == set(PROCESSING_ALGORITHMS)
    for entry in entries:
        operation = operation_from_dict(entry["canonical"])
        assert operation.as_dict() == entry["canonical"]
        authored = type(operation)(**entry["authoring"]["parameters"])
        assert authored == operation


def test_wire_documents_must_be_canonical() -> None:
    payload = GammaOperation(gamma_hundredths=120).as_dict()

    with pytest.raises(ProcessingOperationError, match="unsupported.*schema"):
        operation_from_dict(dict(payload, schema="org.whl.other"))
    with pytest.raises(ProcessingOperationError, match="unsupported.*algorithm"):
        operation_from_dict(dict(payload, algorithm="sepia-v1"))
    with pytest.raises(ProcessingOperationError, match="match gamma-v1 exactly"):
        operation_from_dict(dict(payload, sharpen=True))
    # A restated rule that does not match the recipe's own derivation would let
    # two spellings of one operation produce two command fingerprints.
    with pytest.raises(ProcessingOperationError, match="not a canonical"):
        operation_from_dict(dict(payload, rule="anything else"))


@pytest.mark.parametrize(
    ("operation", "field"),
    [
        (operation, field)
        for operation in _every_operation()
        for field in (
            "schema",
            "version",
            "algorithm",
            "rule",
            *operation.parameters(),
        )
    ],
    ids=lambda value: value.algorithm if hasattr(value, "algorithm") else value,
)
def test_wire_documents_require_every_fixed_and_algorithm_field(
    operation,
    field: str,
) -> None:
    payload = operation.as_dict()
    del payload[field]

    with pytest.raises(ProcessingOperationError):
        operation_from_dict(payload)


def test_operation_lists_are_bounded_and_non_empty() -> None:
    one = GammaOperation().as_dict()

    assert len(operations_from_list([one])) == 1
    with pytest.raises(ProcessingOperationError, match="at least one"):
        operations_from_list([])
    with pytest.raises(ProcessingOperationError, match="at most"):
        operations_from_list([one] * (MAX_OPERATIONS_PER_RECIPE + 1))
    with pytest.raises(ProcessingOperationError, match="must be a sequence"):
        operations_from_list("gamma")


def test_operations_run_before_the_binary_adjustment_in_the_kernel() -> None:
    source = _png(_sample(64, 64))

    plain = apply_perspective_transform(source, FULL_FRAME)
    processed = apply_perspective_transform(
        source,
        FULL_FRAME,
        operations=(GammaOperation(gamma_hundredths=200),),
    )

    assert processed.output_sha256 != plain.output_sha256
    manifest = processed.transform_manifest
    assert [value["algorithm"] for value in manifest["operations"]] == ["gamma-v1"]
    assert manifest["operation_order"] == (
        "applied_in_listed_order_before_adjustment"
    )
    assert apply_perspective_transform(source, FULL_FRAME).transform_manifest[
        "operations"
    ] == []


def test_operations_compose_with_a_mask_and_an_adjustment() -> None:
    result = apply_perspective_transform(
        _png(_sample(64, 64)),
        FULL_FRAME,
        operations=(GammaOperation(gamma_hundredths=160),),
        mask_polygon=((0.1, 0.1), (0.9, 0.2), (0.5, 0.9)),
    )

    with Image.open(io.BytesIO(result.output_png)) as display:
        assert display.mode == "RGBA"
        assert display.getpixel((0, 0))[3] == 0
    assert result.ocr_ready_png != result.output_png


def test_the_kernel_rejects_a_non_operation() -> None:
    with pytest.raises(TypeError, match="ProcessingOperation"):
        apply_perspective_transform(
            _png(_sample()),
            FULL_FRAME,
            operations=({"algorithm": "gamma-v1"},),
        )
