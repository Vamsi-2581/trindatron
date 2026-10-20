"""Development image engine — lets the whole media pipeline run locally.

It draws a deterministic abstract image from the prompt (a gradient seeded by
the prompt's hash) so jobs, storage, quotas, the gallery and every client can
be exercised end to end on a laptop with no GPU. It is not a generative model
and does not pretend to be one: the registry refuses it when
ENVIRONMENT=production, so it can never be what a customer receives.
"""
from __future__ import annotations

import hashlib
import io
import time

from src.media.engines import EngineUnavailable, ImageEngine, ImageRequest, MediaOutput


class DevelopmentImageEngine(ImageEngine):
    def __init__(self, *, production: bool) -> None:
        self._production = production

    async def is_available(self, *, editing: bool = False) -> bool:
        return not self._production

    async def generate(self, request: ImageRequest) -> MediaOutput:
        if self._production:
            raise EngineUnavailable("development engine refused in production")
        from PIL import Image, ImageDraw

        started = time.monotonic()
        digest = hashlib.sha256(f"{request.prompt}|{request.seed}".encode()).digest()
        width, height = min(request.width, 1024), min(request.height, 1024)
        start = (digest[0], digest[1], digest[2])
        end = (digest[3], digest[4], digest[5])
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            t = y / max(1, height - 1)
            draw.line([(0, y), (width, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(start, end)))
        if request.source is not None:
            source = Image.open(io.BytesIO(request.source)).convert("RGB").resize((width, height))
            image = Image.blend(source, image, 0.35)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return MediaOutput(data=buffer.getvalue(), content_type="image/png", width=width, height=height,
                           duration_seconds=None, engine_seconds=time.monotonic() - started)
