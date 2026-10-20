"""
Activity Stream — translates internal operations into human-readable status events.

Rules:
- Never expose model names, provider names, token counts, or costs.
- Never expose tool arguments (they may contain user data or system paths).

Protocol:
  Each SSE status event carries two fields for i18n:
    key     — semantic dot-notation key (e.g. "activity.research.searching")
              Clients that support i18n translate this to the user's locale.
    message — PT-BR fallback string kept for backward compatibility with
              clients that have not adopted key-based translation yet
              (e.g. Electron desktop). Clients SHOULD prefer `key` over `message`.

Adding a new language: add translations to the frontend messages/*.json files.
Adding a new tool: add its key to _TOOL_KEYS (and translations to messages/*.json).
"""
from __future__ import annotations

import json

from src.capability.classifier import Capability

# Semantic keys — resolved to locale strings by the client.
# Key schema: activity.<domain>.<action>
_CAPABILITY_KEYS: dict[Capability, str] = {
    Capability.CHAT:       "activity.chat.processing",
    Capability.RESEARCH:   "activity.research.searching",
    Capability.CODE:       "activity.code.analyzing",
    Capability.WORK:       "activity.work.preparing",
    Capability.IMAGE:      "activity.image.preparing",
    Capability.VIDEO:      "activity.video.preparing",
    Capability.AUDIO:      "activity.audio.preparing",
    Capability.FILES:      "activity.files.accessing",
    Capability.AUTOMATION: "activity.automation.configuring",
    Capability.AGENT:      "activity.agent.starting",
}

_TOOL_KEYS: dict[str, str] = {
    "read_file":          "activity.tool.read_file",
    "write_file":         "activity.tool.write_file",
    "list_directory":     "activity.tool.list_directory",
    "execute_shell":      "activity.tool.execute_shell",
    "web_search":         "activity.tool.web_search",
    "browser_navigate":   "activity.tool.browser_navigate",
    "browser_get_text":   "activity.tool.browser_get_text",
    "browser_screenshot": "activity.tool.browser_screenshot",
    "browser_click":      "activity.tool.browser_click",
    "browser_fill":       "activity.tool.browser_fill",
    "desktop_screenshot": "activity.tool.desktop_screenshot",
    "desktop_get_windows": "activity.tool.desktop_get_windows",
    "desktop_move":       "activity.tool.desktop_move",
    "desktop_click":      "activity.tool.desktop_click",
    "desktop_double_click": "activity.tool.desktop_click",
    "desktop_right_click": "activity.tool.desktop_click",
    "desktop_type":       "activity.tool.desktop_type",
    "desktop_key":        "activity.tool.desktop_type",
    "desktop_scroll":     "activity.tool.desktop_scroll",
    "desktop_drag":       "activity.tool.desktop_move",
    "desktop_open":       "activity.tool.desktop_open",
    "create_document":    "activity.work.creating",
    "send_notification":  "activity.work.approval_required",
    "schedule_action":    "activity.work.approval_required",
}

# PT-BR fallback strings — used as `message` field for backward compat.
_CAPABILITY_MESSAGES: dict[Capability, str] = {
    Capability.CHAT:       "Processando",
    Capability.RESEARCH:   "Pesquisando informações",
    Capability.CODE:       "Analisando código",
    Capability.WORK:       "Preparando tarefa",
    Capability.IMAGE:      "Preparando geração de imagem",
    Capability.VIDEO:      "Preparando geração de vídeo",
    Capability.AUDIO:      "Preparando áudio",
    Capability.FILES:      "Acessando arquivos",
    Capability.AUTOMATION: "Configurando automação",
    Capability.AGENT:      "Iniciando agente",
}

_TOOL_MESSAGES: dict[str, str] = {
    "read_file":          "Lendo arquivo",
    "write_file":         "Salvando arquivo",
    "list_directory":     "Listando diretório",
    "execute_shell":      "Executando comando",
    "web_search":         "Pesquisando na web",
    "browser_navigate":   "Navegando na web",
    "browser_get_text":   "Lendo página",
    "browser_screenshot": "Capturando tela",
    "browser_click":      "Clicando na página",
    "browser_fill":       "Preenchendo formulário",
    "desktop_screenshot": "Capturando tela",
    "desktop_get_windows": "Verificando janelas",
    "desktop_move":       "Movendo o cursor",
    "desktop_click":      "Clicando",
    "desktop_double_click": "Clicando",
    "desktop_right_click": "Clicando",
    "desktop_type":       "Digitando",
    "desktop_key":        "Digitando",
    "desktop_scroll":     "Rolando a tela",
    "desktop_drag":       "Movendo o cursor",
    "desktop_open":       "Abrindo aplicativo",
    "create_document":    "Criando documento",
    "send_notification":  "Aguardando aprovação",
    "schedule_action":    "Aguardando aprovação",
}


def status_event(key: str, message: str) -> str:
    """Build a raw SSE data line for a status event with both key and PT-BR fallback."""
    return f"data: {json.dumps({'type': 'status', 'key': key, 'message': message})}\n\n"


def capability_status_event(capability: Capability) -> str:
    """Return an SSE status event for the given capability."""
    key = _CAPABILITY_KEYS.get(capability, "activity.chat.processing")
    message = _CAPABILITY_MESSAGES.get(capability, "Processando")
    return status_event(key, message)


def tool_activity(tool_name: str) -> dict[str, str]:
    """What a client may know about a tool call: an i18n key and a PT-BR
    label. Never the internal tool name — tools are implementation detail,
    like models and providers."""
    return {
        "activity": _TOOL_KEYS.get(tool_name, "activity.tool.generic"),
        "label": _TOOL_MESSAGES.get(tool_name, "Executando ação"),
    }


def tool_status_event(tool_name: str) -> str:
    """Return an SSE status event describing a tool execution in plain language."""
    key = _TOOL_KEYS.get(tool_name, "activity.tool.generic")
    message = _TOOL_MESSAGES.get(tool_name, "Executando ação")
    return status_event(key, message)
