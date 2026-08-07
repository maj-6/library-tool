"""The capture geometry remap math against PIL/OpenCV ground truth."""

import io
import math

import pytest

from librarytool.processing import capture_geometry as cg


def _marker_image(width=60, height=40, marker=(17, 11)):
    from PIL import Image

    image = Image.new("RGB", (width, height), (10, 10, 10))
    image.putpixel(marker, (250, 40, 40))
    return image


@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
def test_exif_inverse_matches_pillow(orientation):
    """upright_to_raw_point must invert exactly what exif_transpose renders."""
    from PIL import ImageOps

    raw_width, raw_height = 60, 40
    marker = (17, 11)
    image = _marker_image(raw_width, raw_height, marker)
    image.getexif()[0x0112] = orientation
    upright = ImageOps.exif_transpose(image)
    positions = [
        (x, y)
        for x in range(upright.width)
        for y in range(upright.height)
        if upright.getpixel((x, y))[0] > 200
    ]
    assert len(positions) == 1
    upright_x, upright_y = positions[0]
    # Normalized pixel-center coordinates survive the mapping.
    normalized = (
        (upright_x + 0.5) / upright.width,
        (upright_y + 0.5) / upright.height,
    )
    raw_x, raw_y = cg.upright_to_raw_point(*normalized, orientation)
    assert raw_x * raw_width == pytest.approx(marker[0] + 0.5, abs=1e-9)
    assert raw_y * raw_height == pytest.approx(marker[1] + 0.5, abs=1e-9)


def test_jpeg_exif_orientation_roundtrip():
    from PIL import Image

    buffer = io.BytesIO()
    image = _marker_image()
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(buffer, "JPEG", exif=exif.tobytes())
    assert cg.jpeg_exif_orientation(buffer.getvalue()) == 6
    assert cg.jpeg_exif_orientation(b"not an image") == 1


def test_perspective_matrix_matches_opencv():
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    quad = ((40.0, 30.0), (500.0, 55.0), (520.0, 700.0), (25.0, 660.0))
    destination = ((0.0, 0.0), (459.0, 0.0), (459.0, 649.0), (0.0, 649.0))
    mine = cg.perspective_matrix(quad, destination)
    reference = cv2.getPerspectiveTransform(
        np.array(quad, dtype=np.float32),
        np.array(destination, dtype=np.float32),
    )
    for row in range(3):
        for column in range(3):
            assert mine[row][column] == pytest.approx(
                float(reference[row][column]), rel=1e-5, abs=1e-7
            )
    # And point application agrees with cv2.perspectiveTransform.
    points = [(100.0, 100.0), (321.5, 447.25)]
    projected = cv2.perspectiveTransform(
        np.array([points], dtype=np.float64), reference
    )[0]
    for point, expected in zip(points, projected):
        got = cg.apply_matrix(mine, *point)
        assert got[0] == pytest.approx(float(expected[0]), abs=1e-6)
        assert got[1] == pytest.approx(float(expected[1]), abs=1e-6)


def test_warp_destination_size_matches_compat_kernel():
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from librarytool.processing.raster import (
        apply_capture_pixel_perspective_compat,
    )

    quad = np.array(
        [[40.0, 30.0], [500.0, 55.0], [520.0, 700.0], [25.0, 660.0]],
        dtype=np.float32,
    )
    source = np.zeros((800, 600, 3), dtype=np.uint8)
    encoded = cv2.imencode(".jpg", source)[1].tobytes()
    output = apply_capture_pixel_perspective_compat(encoded, quad)
    decoded = cv2.imdecode(
        np.frombuffer(output, dtype=np.uint8), cv2.IMREAD_COLOR
    )
    height, width = decoded.shape[:2]
    assert cg.capture_warp_destination_size(quad.tolist()) == (width, height)


def test_remap_without_quad_is_identity():
    regions = [{"id": "r0", "polygon": [[0.1, 0.2], [0.9, 0.2], [0.9, 0.4]]}]
    remapped, dropped = cg.remap_phone_regions(
        regions,
        source_size=(600, 800),
        quad=None,
        warp_size=None,
    )
    assert dropped == 0
    assert remapped[0]["polygon"] == regions[0]["polygon"]
    assert remapped[0] is not regions[0]


def test_remap_through_axis_aligned_crop():
    # A rectangular quad makes the warp a pure crop+scale, so expected
    # coordinates are computable by hand: crop x in [100, 500), y in
    # [200, 600) of an 800x1000 original.
    quad = ((100.0, 200.0), (500.0, 200.0), (500.0, 600.0), (100.0, 600.0))
    warp_size = cg.capture_warp_destination_size(quad)
    assert warp_size == (400, 400)
    regions = [
        {"id": "inside", "polygon": [[0.25, 0.3], [0.5, 0.3], [0.5, 0.5], [0.25, 0.5]]},
        {"id": "outside", "polygon": [[0.9, 0.9], [0.95, 0.9], [0.95, 0.95], [0.9, 0.95]]},
    ]
    remapped, dropped = cg.remap_phone_regions(
        regions,
        source_size=(800, 1000),
        quad=quad,
        warp_size=warp_size,
    )
    # "outside" clamps to the far corner and collapses.
    assert dropped == 1
    assert [region["id"] for region in remapped] == ["inside"]
    polygon = remapped[0]["polygon"]
    # x=0.25 of 800px = 200px -> (200-100)/399 scaled by the [0..w-1]
    # destination mapping, normalized by 400.
    expected_x = ((0.25 * 800 - 100.0) * (399.0 / 400.0)) / 400.0
    expected_y = ((0.3 * 1000 - 200.0) * (399.0 / 400.0)) / 400.0
    assert polygon[0][0] == pytest.approx(expected_x, abs=1e-9)
    assert polygon[0][1] == pytest.approx(expected_y, abs=1e-9)


def test_remap_does_not_re_apply_exif_orientation():
    """The remap must treat coordinates as already EXIF-upright.

    cv2.imdecode applies the EXIF orientation itself, so the detector quad
    and the phone's normalized regions are in the SAME upright frame. An
    orientation correction here would rotate boxes off the text; this test
    pins the frame by asserting a plain full-frame homography result.
    """
    quad = ((0.0, 0.0), (799.0, 0.0), (799.0, 599.0), (0.0, 599.0))
    warp_size = cg.capture_warp_destination_size(quad)
    regions = [{"id": "r", "polygon": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.2]]}]
    remapped, dropped = cg.remap_phone_regions(
        regions,
        source_size=(800, 600),
        quad=quad,
        warp_size=warp_size,
    )
    assert dropped == 0
    # The kernel maps source [0..size-1] onto destination [0..size-1]; a
    # full-frame quad is therefore a pure rescale, with NO axis swap.
    assert remapped[0]["polygon"][0][0] == pytest.approx(
        0.1 * 800.0 * (warp_size[0] - 1) / 799.0 / warp_size[0], abs=1e-6)
    assert remapped[0]["polygon"][0][1] == pytest.approx(
        0.1 * 600.0 * (warp_size[1] - 1) / 599.0 / warp_size[1], abs=1e-6)


def test_cv2_imdecode_applies_exif_so_the_quad_is_upright():
    """Pin the orientation fact the whole remap contract rests on.

    If a future OpenCV stops applying EXIF on decode, the detector quad
    would silently move to the as-stored grid and every warped remap would
    be wrong by a rotation. Fail loudly here instead.
    """
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from PIL import Image, ImageOps

    image = Image.new("RGB", (60, 40), (10, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes(), quality=95)
    data = buffer.getvalue()

    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    with Image.open(io.BytesIO(data)) as opened:
        upright = ImageOps.exif_transpose(opened).size
    assert (decoded.shape[1], decoded.shape[0]) == upright, (
        "cv2.imdecode no longer applies EXIF; capture_geometry's frame "
        "assumption and process_photo_traced's source_size are now wrong")


def test_traced_derivation_reports_the_upright_source_frame():
    """process_photo_traced must measure the frame cv2 actually saw."""
    pytest.importorskip("cv2")
    from PIL import Image

    import capture_pipeline

    image = Image.new("RGB", (60, 40), (120, 90, 60))
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes(), quality=95)

    _final, trace = capture_pipeline.process_photo_traced(buffer.getvalue())
    # Stored is 60x40; upright (what cv2 and standardize both see) is 40x60.
    assert trace["source_size"] == (40, 60)
    assert trace["exif_orientation"] == 6


def test_desktop_record_pins_and_preserves_provenance():
    phone = {
        "asset_id": "a1",
        "coordinate_space": "display_normalized",
        "engine": "mistral",
        "engine_version": "ocr-4-blocks",
        "model": "mistral-ocr-latest",
        "source_revision": 1,
        "display_revision": 1,
        "source_sha256": "ab" * 32,
        "width": 1536,
        "height": 2048,
        "orientation": 0,
        "regions": [{"id": "r0", "polygon": [[0, 0], [1, 0], [1, 1]]}],
    }
    record = cg.desktop_display_geometry_record(
        phone,
        regions=phone["regions"],
        display_width=1200,
        display_height=1600,
        display_sha256="cd" * 32,
    )
    assert record["engine"] == "mistral"
    assert record["source_sha256"] == "ab" * 32
    assert record["display_sha256"] == "cd" * 32
    assert (record["width"], record["height"]) == (1200, 1600)
    assert record["orientation"] == 0
    assert record["remap_recipe"] == cg.DESKTOP_REMAP_RECIPE
    assert record["regions"] is not phone["regions"]
    with pytest.raises(ValueError):
        cg.desktop_display_geometry_record(
            phone, regions=[], display_width=0, display_height=1,
            display_sha256="cd" * 32,
        )
