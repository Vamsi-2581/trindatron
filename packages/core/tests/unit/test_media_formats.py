"""A format asked for in words becomes a proportion and, when given, exact pixels — never one of three buttons."""
from __future__ import annotations

import pytest

from src.media import formats
from src.media.xai import IMAGE_RATIOS, VIDEO_RATIOS, nearest_ratio


@pytest.mark.parametrize("text,label,size,source", [
    ("Crie uma imagem vertical 1080x1920 de um farol", "9:16", "1080x1920", "pixels"),
    ("an image 1920 x 1080 of a harbour", "16:9", "1920x1080", "pixels"),
    ("imagem 1024×1024", "1:1", "1024x1024", "pixels"),
    ("banner em proporção 4:5", "4:5", None, "ratio"),
    ("uma arte 9:16 para o celular", "9:16", None, "ratio"),
    ("Crie um post para Instagram Story sobre café", "9:16", "1080x1920", "story"),
    ("post do instagram com o logo", "4:5", "1080x1350", "instagram_post"),
    ("thumbnail para o YouTube", "16:9", "1280x720", "thumbnail"),
    ("capa do linkedin da empresa", "4:1", "1584x396", "linkedin_banner"),
    ("um banner para o site", "3:1", "1500x500", "banner"),
    ("pin para pinterest de receitas", "2:3", "1000x1500", "pinterest"),
    ("imagem em retrato de um gato", "3:4", None, "orientation"),
    ("foto panorâmica da praia", "4:3", None, "orientation"),
    ("um gato astronauta", "1:1", None, "default"),
])
def test_image_requests_become_structured_formats(text, label, size, source):
    spec = formats.parse_format(text, "image")
    assert spec.label == label
    assert spec.source == source
    assert spec.as_params().get("requested_size") == size


def test_video_defaults_and_orientation_follow_video_conventions():
    assert formats.parse_format("um vídeo de ondas", "video").label == "16:9"
    assert formats.parse_format("vídeo vertical de ondas", "video").label == "9:16"


@pytest.mark.parametrize("text", ["um vídeo sobre o feed de notícias", "imagem de um banner de feed"])
def test_ordinary_words_do_not_switch_the_format_to_a_social_preset(text):
    assert formats.parse_format(text, "image").source != "instagram_post"


def test_a_time_of_day_is_not_mistaken_for_a_ratio():
    assert formats.parse_format("uma imagem de um relógio marcando 10:30", "image").label == "1:1"


def test_implausible_sizes_fall_back_instead_of_breaking():
    assert formats.parse_format("imagem 100x9000", "image").label == "1:1"


def test_fit_keeps_the_proportion_and_never_exceeds_what_was_asked_or_allowed():
    story = formats.parse_format("1080x1920", "image")
    width, height = formats.fit(story, long_side=1024)
    assert height == 1024 and abs(width / height - 9 / 16) < 0.03
    small = formats.parse_format("512x512", "image")
    assert formats.fit(small, long_side=1024) == (512, 512)
    wide = formats.parse_format("capa do linkedin", "image")
    width, height = formats.fit(wide, long_side=1024)
    assert width == 1024 and abs(width / height - 4) < 0.1


def test_legacy_shape_names_and_explicit_pixels_from_an_api_body():
    assert formats.from_request(kind="image", aspect="portrait").label == "3:4"
    assert formats.from_request(kind="video", aspect="portrait").label == "9:16"
    assert formats.from_request(kind="image", width=1200, height=628).label == "300:157"


@pytest.mark.parametrize("width,height,ratios,expected", [
    (1080, 1920, IMAGE_RATIOS, "9:16"),
    (1080, 1350, IMAGE_RATIOS, "3:4"),
    (1584, 396, IMAGE_RATIOS, "5:2"),
    (1500, 500, VIDEO_RATIOS, "16:9"),
    (1024, 1024, VIDEO_RATIOS, "1:1"),
])
def test_an_engine_serves_the_nearest_shape_it_can_make(width, height, ratios, expected):
    assert nearest_ratio(width, height, ratios) == expected
