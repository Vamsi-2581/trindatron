"""The person's words become the scene an image or video model follows; any failure keeps their words."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.media import prompt_writer

SCENE = ("Wide cinematic shot at golden hour on Lago do Manso, Mato Grosso, Brazil: a small wooden boat glides "
         "across calm water, low hills of cerrado behind, the sky orange and pink. Slow dolly. Photorealistic.")


class Router:
    def __init__(self, content: str | None = SCENE, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.content, self.error, self.delay, self.requests = content, error, delay, []

    async def complete(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content, model="cheap-writer", cost_usd=0.00042, tcu=0.042)


async def test_a_request_becomes_a_detailed_scene_with_its_cost():
    router = Router()
    written = await prompt_writer.rewrite("crie um video de um barco no manso mt no po do sol", kind="video",
                                          plan="max", router=router)

    assert written.prompt == SCENE and written.cost_usd == pytest.approx(0.00042) and written.model == "cheap-writer"
    [request] = router.requests
    system, user = request.messages
    assert "crie um video de um barco no manso mt no po do sol" in user["content"] and "in English" in user["content"]
    assert "Lago do Manso" in system["content"] and "camera movement" in system["content"]
    assert "leave out sounds" in system["content"]  # a silent engine is never told to make sound
    assert request.max_output_tokens == prompt_writer.MAX_OUTPUT_TOKENS
    assert request.max_cost_usd_override == prompt_writer.MAX_COST_USD


async def test_premium_with_sound_keeps_the_words_someone_says():
    router = Router()
    await prompt_writer.rewrite("um homem gritando 'VIVA A VIDA, TRINDATRON!'", kind="video", plan="max",
                                with_sound=True, router=router)
    system = router.requests[0].messages[0]["content"]
    assert "Describe the sounds too" in system and "original language" in system


async def test_an_image_prompt_has_no_camera_movement():
    router = Router()
    await prompt_writer.rewrite("um farol à noite", kind="image", plan="start", router=router)
    system = router.requests[0].messages[0]["content"]
    assert "text-to-image" in system and "camera movement" not in system


@pytest.mark.parametrize("router", [
    Router(error=RuntimeError("every provider is down")),
    Router(content="   "),
    Router(content="x" * (prompt_writer.MAX_PROMPT_CHARS + 1)),
])
async def test_any_failure_leaves_the_persons_own_words(router):
    assert await prompt_writer.rewrite("um farol", kind="image", plan="start", router=router) is None


async def test_a_slow_writer_never_holds_the_creation(monkeypatch):
    monkeypatch.setattr(prompt_writer, "TIMEOUT_S", 0.05)
    assert await prompt_writer.rewrite("um farol", kind="image", plan="start", router=Router(delay=1.0)) is None


@pytest.mark.parametrize("answer, expected", [
    ('"A lighthouse at night."', "A lighthouse at night."),
    ("Prompt: A lighthouse at night.", "A lighthouse at night."),
    ("A lighthouse\n at   night.", "A lighthouse at night."),
])
async def test_the_answer_is_cleaned_to_one_line(answer, expected):
    written = await prompt_writer.rewrite("farol", kind="image", plan="start", router=Router(answer))
    assert written.prompt == expected


async def test_without_an_injected_router_tests_never_reach_a_provider():
    assert await prompt_writer.rewrite("um farol", kind="image", plan="start") is None
