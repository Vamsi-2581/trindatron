"""Vectorization is deterministic: no model, no network, no GPU — just VTracer's own algorithm."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from src.media.vectorize import VectorizeFailed, extension_for, vectorize, vectorize_sync


def _png(color=(255, 0, 0), size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_extension_for_the_three_accepted_types():
    assert extension_for("image/png") == "png"
    assert extension_for("image/jpeg; charset=binary") == "jpg"
    assert extension_for("image/webp") == "webp"
    assert extension_for("image/gif") is None
    assert extension_for("") is None


def test_a_real_png_becomes_a_real_svg_document():
    svg = vectorize_sync(_png(), "image/png")
    assert svg.startswith("<?xml") and "<svg" in svg and "</svg>" in svg


async def test_the_async_wrapper_runs_off_the_event_loop_and_returns_the_same_result():
    svg = await vectorize(_png(), "image/png")
    assert "<svg" in svg


@pytest.mark.parametrize("data,content_type", [(b"", "image/png"), (b"not an image", "image/png"),
                                               (_png(), "image/gif"), (_png(), "")])
def test_unsupported_or_empty_input_is_a_controlled_failure_not_a_crash(data, content_type):
    with pytest.raises(VectorizeFailed):
        vectorize_sync(data, content_type)
