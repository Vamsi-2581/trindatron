"""The shape a person asks for, in their own words, as structured metadata.

"1080x1920", "9:16", "Instagram Story", "banner do YouTube", "vertical" — never a
choice between three buttons. `parse_format` reads the request; `fit` turns the
wanted proportion into the dimensions a plan allows. Engines then map those
dimensions to the nearest shape they can produce (media/xai.py `nearest_ratio`,
media/hosted.py `_size`), so an unusual request is served as closely as the
engine allows instead of being refused.

Precedence: explicit pixels > explicit ratio > named format > orientation word >
the default for the kind of creation.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FormatSpec:
    ratio_w: float
    ratio_h: float
    # The exact pixels asked for, when the person gave them.
    requested_width: int | None = None
    requested_height: int | None = None
    # Where it came from: "pixels" | "ratio" | a preset name | "orientation" | "default".
    source: str = "default"

    @property
    def ratio(self) -> float:
        return self.ratio_w / self.ratio_h

    @property
    def label(self) -> str:
        def number(value: float) -> str:
            return str(int(value)) if float(value).is_integer() else f"{value:g}"

        return f"{number(self.ratio_w)}:{number(self.ratio_h)}"

    def as_params(self) -> dict:
        params: dict = {"format": self.label, "format_source": self.source}
        if self.requested_width and self.requested_height:
            params["requested_size"] = f"{self.requested_width}x{self.requested_height}"
        return params


# Named formats, longest phrases first so "instagram story" wins over "instagram".
_PRESETS: list[tuple[re.Pattern, str, tuple[int, int] | None, tuple[float, float]]] = [
    (re.compile(p, re.IGNORECASE), name, size, ratio) for p, name, size, ratio in [
        (r"\b(instagram|insta|ig)\s*(stor(y|ies)|reels?)\b|\bstor(y|ies)\b|\breels?\b|\btik\s*tok\b|\bshorts\b",
         "story", (1080, 1920), (9, 16)),
        (r"\b(instagram|insta|ig)\s*(post|feed)\b|\bpost\s+(do|para o|para|for)\s+(o\s+)?instagram\b|\bfeed\s+do\s+instagram\b",
         "instagram_post", (1080, 1350), (4, 5)),
        (r"\b(capa|banner|header)\s+(do|para o|de|for|on)?\s*(youtube|canal)\b|\byoutube\s+(banner|capa|header)\b",
         "youtube_banner", (2560, 1440), (16, 9)),
        (r"\b(thumbnail|thumb|miniatura)\b", "thumbnail", (1280, 720), (16, 9)),
        (r"\byoutube\b|\bwidescreen\b|\bfull\s*hd\b|\b1080p\b", "youtube", (1920, 1080), (16, 9)),
        (r"\b(capa|banner|header)\s+(do|para o|de|for|on)?\s*linkedin\b|\blinkedin\s+(banner|capa|header)\b",
         "linkedin_banner", (1584, 396), (4, 1)),
        (r"\b(capa|cover)\s+(do|para o|de|for|on)?\s*facebook\b|\bfacebook\s+cover\b", "facebook_cover", (1640, 624), (820, 312)),
        (r"\b(capa|header|banner)\s+(do|para o|de|for|on)?\s*(twitter|x)\b|\btwitter\s+(header|banner)\b",
         "x_header", (1500, 500), (3, 1)),
        (r"\bbanner\b", "banner", (1500, 500), (3, 1)),
        (r"\bpinterest\b|\bpin\b", "pinterest", (1000, 1500), (2, 3)),
        (r"\b(papel de parede|wallpaper)\s+(de|do|para|for)?\s*(celular|telefone|phone|m[óo]vil)\b",
         "phone_wallpaper", (1170, 2532), (9, 19.5)),
        (r"\b(papel de parede|wallpaper)\b", "wallpaper", (2560, 1440), (16, 9)),
        (r"\bcinema(tic|togr[aá]fico)?\b|\bultrawide\b|\b21\s*:\s*9\b", "cinema", None, (21, 9)),
        (r"\b(a4|a3|a5|folha|p[oô]ster|poster|cartaz)\b", "poster", None, (1, math.sqrt(2))),
        (r"\b(quadrad[oa]|square|cuadrad[oa])\b", "square", None, (1, 1)),
    ]
]

_PIXELS = re.compile(r"\b(\d{3,5})\s*[x×*]\s*(\d{3,5})(?:\s*(?:px|pixels?))?\b", re.IGNORECASE)
_RATIO = re.compile(r"\b(\d{1,2}(?:[.,]\d{1,2})?)\s*[:/]\s*(\d{1,2}(?:[.,]\d{1,2})?)\b")
_FORMAT_WORDS = re.compile(r"(propor[çc][ãa]o|formato|aspect|ratio|relaci[óo]n|tamanho|size)", re.IGNORECASE)
_COMMON_RATIOS = {(1, 1), (4, 5), (5, 4), (3, 4), (4, 3), (2, 3), (3, 2), (9, 16), (16, 9), (21, 9), (9, 21),
                  (1, 2), (2, 1), (3, 1), (1, 3), (4, 1), (1, 4), (5, 7), (7, 5), (9, 19.5), (19.5, 9),
                  (2.35, 1), (1.91, 1), (5, 2), (2, 5)}
_PORTRAIT = re.compile(r"\b(vertical|retrato|portrait|em p[ée])\b", re.IGNORECASE)
_LANDSCAPE = re.compile(r"\b(horizontal|paisagem|paisaje|landscape|deitad[oa]|panor[aâ]mic[oa]|panoramic)\b", re.IGNORECASE)

# Legacy request values from clients that still send one of three shapes.
LEGACY_ASPECTS = {"square": (1.0, 1.0), "portrait": (3.0, 4.0), "landscape": (4.0, 3.0)}
LEGACY_VIDEO_ASPECTS = {"square": (1.0, 1.0), "portrait": (9.0, 16.0), "landscape": (16.0, 9.0)}

MIN_RATIO, MAX_RATIO = 1 / 4, 4.0


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _plausible(w: float, h: float) -> bool:
    return w > 0 and h > 0 and MIN_RATIO <= w / h <= MAX_RATIO


def parse_format(text: str, kind: str = "image") -> FormatSpec:
    """The format asked for in `text`, or the default for `kind`. Never raises."""
    content = text or ""

    match = _PIXELS.search(content)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if 64 <= width <= 8192 and 64 <= height <= 8192 and _plausible(width, height):
            divisor = math.gcd(width, height)
            return FormatSpec(width / divisor, height / divisor, width, height, "pixels")

    for match in _RATIO.finditer(content):
        w, h = _number(match.group(1)), _number(match.group(2))
        near_word = _FORMAT_WORDS.search(content[max(0, match.start() - 24):match.start()])
        if _plausible(w, h) and ((w, h) in _COMMON_RATIOS or near_word):
            return FormatSpec(w, h, None, None, "ratio")

    for pattern, name, size, (w, h) in _PRESETS:
        if pattern.search(content):
            return FormatSpec(float(w), float(h), size[0] if size else None, size[1] if size else None, name)

    if _PORTRAIT.search(content):
        return FormatSpec(9.0, 16.0, source="orientation") if kind == "video" else FormatSpec(3.0, 4.0, source="orientation")
    if _LANDSCAPE.search(content):
        return FormatSpec(16.0, 9.0, source="orientation") if kind == "video" else FormatSpec(4.0, 3.0, source="orientation")
    return FormatSpec(16.0, 9.0) if kind == "video" else FormatSpec(1.0, 1.0)


def from_request(*, kind: str, format_text: str | None = None, aspect: str | None = None,
                 width: int | None = None, height: int | None = None) -> FormatSpec:
    """A format from an API body: explicit size, then free text, then a legacy aspect name."""
    if width and height and _plausible(width, height):
        divisor = math.gcd(int(width), int(height))
        return FormatSpec(width / divisor, height / divisor, int(width), int(height), "pixels")
    if format_text and format_text.strip():
        return parse_format(format_text, kind)
    legacy = (LEGACY_VIDEO_ASPECTS if kind == "video" else LEGACY_ASPECTS).get((aspect or "").strip().lower())
    if legacy:
        return FormatSpec(*legacy, source="orientation" if aspect != "square" else "square")
    return parse_format("", kind)


def fit(spec: FormatSpec, *, long_side: int, multiple: int = 16) -> tuple[int, int]:
    """Dimensions with the spec's proportion, the long side at most `long_side` (and at most what was asked)."""
    wanted_long = max(spec.requested_width or 0, spec.requested_height or 0)
    target = min(long_side, wanted_long) if wanted_long else long_side
    ratio = spec.ratio
    if ratio >= 1:
        width, height = target, target / ratio
    else:
        width, height = target * ratio, target
    width = max(multiple, int(width) - int(width) % multiple)
    height = max(multiple, int(height) - int(height) % multiple)
    return width, height


def fit_short_side(spec: FormatSpec, *, short_side: int, multiple: int = 16) -> tuple[int, int]:
    """Video dimensions: the short side at the plan's resolution, the proportion kept."""
    ratio = spec.ratio
    if ratio >= 1:
        width, height = short_side * ratio, short_side
    else:
        width, height = short_side, short_side / ratio
    width = max(multiple, int(width) - int(width) % multiple)
    height = max(multiple, int(height) - int(height) % multiple)
    return width, height
