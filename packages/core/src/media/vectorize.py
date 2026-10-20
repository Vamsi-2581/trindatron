"""Vectorization: raster image in, SVG text out — deterministic, never a generative model.

CP18. VTracer (MIT, github.com/visioncortex/vtracer) is a classical algorithm — hierarchical
clustering plus curve fitting, not a neural network — so unlike every other media capability in
this package it needs no model weights, no GPU and no network call, and its result does not vary
run to run for the same input and settings. That is why the actual conversion is a plain function,
not routed through a Runner: there is no infrastructure to choose between, no GPU cost to reserve.

CP19: `VTracerEngine` below makes it a real `OperationEngine` (operation="vectorize") — the same
permanent TRANSFORM contract BiRefNet uses (media/operations.py) — so it is discoverable and
registerable the same way. `POST /media/vectorize` (api/routes/media.py) and
`media_service.vectorize_source_image()` still call it directly and synchronously rather than
through the MediaJob queue: forcing a free, <100ms, always-succeeds-or-fails-immediately operation
through async polling would add latency for no benefit, not fix anything. This is a deliberate,
documented exception to "every operation goes through the job queue" — not a leftover shortcut.

License: MIT (github.com/visioncortex/vtracer/blob/master/LICENSE) — unconditional commercial use.
"""
from __future__ import annotations

import asyncio

ALLOWED_INPUT_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# VTracer's own accepted `img_format` values (github.com/visioncortex/vtracer README).
_VTRACER_FORMAT = {"png": "png", "jpg": "jpeg", "webp": "webp"}


class VectorizeFailed(Exception):
    """The input could not be vectorized. `reason` is for logs only, never shown to a user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def extension_for(content_type: str) -> str | None:
    """The input extension VTracer accepts for `content_type`, or None when it does not."""
    return ALLOWED_INPUT_TYPES.get((content_type or "").split(";")[0].strip().lower())


def vectorize_sync(data: bytes, content_type: str) -> str:
    """Convert one raster image to an SVG document. Raises VectorizeFailed; never returns partial output."""
    extension = extension_for(content_type)
    if extension is None or not data:
        raise VectorizeFailed("unsupported or empty input")
    import vtracer

    try:
        svg = vtracer.convert_raw_image_to_svg(bytes(data), img_format=_VTRACER_FORMAT[extension])
    except Exception as exc:  # the Rust extension raises plain Exception/ValueError on bad input
        raise VectorizeFailed(f"conversion failed: {type(exc).__name__}") from None
    if not svg or "<svg" not in svg:
        raise VectorizeFailed("empty output")
    return svg


async def vectorize(data: bytes, content_type: str) -> str:
    """Async wrapper: VTracer is CPU-bound and synchronous, so it runs off the event loop thread."""
    return await asyncio.to_thread(vectorize_sync, data, content_type)


class VTracerEngine:
    """The `OperationEngine` shape (media/operations.py) for `vectorize` — used directly by
    media_service.vectorize_source_image, not through OperationEngineChain/the job queue (see
    the module docstring). Duck-typed to the ABC rather than subclassing it, to avoid importing
    media/operations.py (and its orchestrator import) into this dependency-free module."""

    operation = "vectorize"

    async def is_available(self) -> bool:
        return True

    def estimate_usd(self, request) -> float:
        return 0.0

    async def run(self, request, resolved: dict):
        from src.media.engines import MediaOutput

        data, content_type = resolved.get("source_image", (b"", ""))
        svg = await vectorize(data, content_type)
        svg_bytes = svg.encode("utf-8")
        return MediaOutput(data=svg_bytes, content_type="image/svg+xml", width=None, height=None,
                           duration_seconds=None, engine_seconds=0.0, cost_usd=0.0)
