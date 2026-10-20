"""Unit tests for the Capability Classifier."""
from __future__ import annotations

import pytest

from src.capability.classifier import Capability, classify


class TestClassifyFallback:
    def test_empty_string_returns_chat(self):
        assert classify("") == Capability.CHAT

    def test_generic_greeting_returns_chat(self):
        assert classify("Hello, how are you?") == Capability.CHAT

    def test_question_without_keywords_returns_chat(self):
        assert classify("What is the capital of France?") == Capability.CHAT


class TestCodeClassification:
    @pytest.mark.parametrize("text", [
        "write a function to sort a list",
        "debug this Python script",
        "refactor my authentication module",
        "implement a binary search algorithm",
        "fix the bug in my code",
        "write a unit test for this class",
    ])
    def test_code_keywords(self, text):
        assert classify(text) == Capability.CODE


class TestResearchClassification:
    @pytest.mark.parametrize("text", [
        "search for the latest news on AI",
        "look up information about climate change",
        "find information about machine learning",
        "search the web for Python tutorials",
        "research this topic for me",
    ])
    def test_research_keywords(self, text):
        assert classify(text) == Capability.RESEARCH


class TestImageClassification:
    @pytest.mark.parametrize("text", [
        "generate an image of a sunset",
        "create an image of a cat",
        "draw a landscape with mountains",
        "illustrate a character for my book",
    ])
    def test_image_keywords(self, text):
        assert classify(text) == Capability.IMAGE


class TestVideoClassification:
    @pytest.mark.parametrize("text", [
        "generate a video of a flying bird",
        "create a video for my product",
        "animate my logo",
        "make me a video of the ocean",
        # Asking for a video the way people ask for one. These reached CHAT before, so the
        # request was answered in words by a model instead of becoming a real creation.
        "Faça um vídeo pra mim a partir dessa foto",
        "faça um vídeo de ondas quebrando",
        "monte um vídeo com essa imagem",
        "quero um vídeo vertical de 5 segundos",
        "haz un video de un faro",
        "quiero un video vertical",
    ])
    def test_video_keywords(self, text):
        assert classify(text) == Capability.VIDEO

    @pytest.mark.parametrize("text", [
        "Crie uma imagem de um urso",
        "faça uma foto de um farol",
        "haz una imagen de un faro",
    ])
    def test_an_image_request_is_still_an_image(self, text):
        assert classify(text) == Capability.IMAGE


class TestAudioClassification:
    @pytest.mark.parametrize("text", [
        "transcribe this audio file",
        "convert text to speech",
        "generate an audio narration",
    ])
    def test_audio_keywords(self, text):
        assert classify(text) == Capability.AUDIO


class TestFilesClassification:
    @pytest.mark.parametrize("text", [
        "read the file config.json",
        "write to the file output.txt",
        "parse the CSV data",
        "open the file and show me its contents",
    ])
    def test_files_keywords(self, text):
        assert classify(text) == Capability.FILES


class TestWorkClassification:
    @pytest.mark.parametrize("text", [
        "browse to https://example.com and click the button",
        "navigate to https://login.example.com",
        "fill in the form with my email",
    ])
    def test_work_keywords(self, text):
        assert classify(text) == Capability.WORK


class TestAutomationClassification:
    @pytest.mark.parametrize("text", [
        "automate this workflow for me",
        "schedule a cron job daily",
        "create a pipeline to process data",
        "set up a webhook for my app",
    ])
    def test_automation_keywords(self, text):
        assert classify(text) == Capability.AUTOMATION


class TestAgentClassification:
    @pytest.mark.parametrize("text", [
        "act as an agent and complete this task",
        "run an agent to process the file",
        "plan and execute the following steps",
    ])
    def test_agent_keywords(self, text):
        assert classify(text) == Capability.AGENT


class TestCaseInsensitivity:
    def test_uppercase_code_keyword(self):
        assert classify("WRITE A FUNCTION to parse JSON") == Capability.CODE

    def test_mixed_case_image_keyword(self):
        assert classify("Generate An Image of a forest") == Capability.IMAGE
