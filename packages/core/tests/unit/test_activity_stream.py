"""Unit tests for the Activity Stream module."""
from __future__ import annotations

import json

import pytest

from src.capability.activity_stream import (
    capability_status_event,
    status_event,
    tool_status_event,
)
from src.capability.classifier import Capability


class TestStatusEvent:
    def test_returns_sse_data_line(self):
        result = status_event("activity.chat.processing", "Processando")
        assert result.startswith("data: ")
        assert result.endswith("\n\n")

    def test_json_contains_key_and_message(self):
        result = status_event("activity.test.key", "Test message")
        payload = json.loads(result.removeprefix("data: ").strip())
        assert payload["type"] == "status"
        assert payload["key"] == "activity.test.key"
        assert payload["message"] == "Test message"


class TestCapabilityStatusEvent:
    @pytest.mark.parametrize("capability,expected_key,expected_message", [
        (Capability.CHAT,       "activity.chat.processing",       "Processando"),
        (Capability.RESEARCH,   "activity.research.searching",    "Pesquisando informações"),
        (Capability.CODE,       "activity.code.analyzing",        "Analisando código"),
        (Capability.WORK,       "activity.work.preparing",        "Preparando tarefa"),
        (Capability.IMAGE,      "activity.image.preparing",       "Preparando geração de imagem"),
        (Capability.VIDEO,      "activity.video.preparing",       "Preparando geração de vídeo"),
        (Capability.AUDIO,      "activity.audio.preparing",       "Preparando áudio"),
        (Capability.FILES,      "activity.files.accessing",       "Acessando arquivos"),
        (Capability.AUTOMATION, "activity.automation.configuring","Configurando automação"),
        (Capability.AGENT,      "activity.agent.starting",        "Iniciando agente"),
    ])
    def test_all_capabilities_emit_key_and_message(self, capability, expected_key, expected_message):
        result = capability_status_event(capability)
        payload = json.loads(result.removeprefix("data: ").strip())
        assert payload["type"] == "status"
        assert payload["key"] == expected_key
        assert payload["message"] == expected_message  # backward compat field

    def test_event_is_valid_sse_format(self):
        result = capability_status_event(Capability.CODE)
        assert result.startswith("data: ")
        assert result.endswith("\n\n")

    def test_key_uses_dot_notation(self):
        result = capability_status_event(Capability.RESEARCH)
        payload = json.loads(result.removeprefix("data: ").strip())
        assert "." in payload["key"]
        assert payload["key"].startswith("activity.")


class TestToolStatusEvent:
    @pytest.mark.parametrize("tool,expected_key,expected_message", [
        ("read_file",        "activity.tool.read_file",        "Lendo arquivo"),
        ("write_file",       "activity.tool.write_file",       "Salvando arquivo"),
        ("list_directory",   "activity.tool.list_directory",   "Listando diretório"),
        ("execute_shell",    "activity.tool.execute_shell",    "Executando comando"),
        ("web_search",       "activity.tool.web_search",       "Pesquisando na web"),
        ("browser_navigate", "activity.tool.browser_navigate", "Navegando na web"),
        ("browser_get_text", "activity.tool.browser_get_text", "Lendo página"),
    ])
    def test_known_tools_emit_key_and_message(self, tool, expected_key, expected_message):
        result = tool_status_event(tool)
        payload = json.loads(result.removeprefix("data: ").strip())
        assert payload["key"] == expected_key
        assert payload["message"] == expected_message

    def test_unknown_tool_returns_generic_key_and_message(self):
        result = tool_status_event("some_unknown_tool")
        payload = json.loads(result.removeprefix("data: ").strip())
        assert payload["type"] == "status"
        assert payload["key"]     # non-empty
        assert payload["message"] # non-empty

    def test_tool_event_never_exposes_tool_name_in_message(self):
        result = tool_status_event("read_file")
        payload = json.loads(result.removeprefix("data: ").strip())
        assert "read_file" not in payload["message"]

    def test_tool_event_is_valid_sse_format(self):
        result = tool_status_event("web_search")
        assert result.startswith("data: ")
        assert result.endswith("\n\n")
