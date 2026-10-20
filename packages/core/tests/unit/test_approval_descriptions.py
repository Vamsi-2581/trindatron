"""An approval names the element as the person sees it, never by its selector (agent_service._describe_tool_action).

On a physical phone the approval card read "Clicar em 'a' no navegador".
"""
import pytest

from src.services.agent_service import _describe_tool_action


@pytest.mark.parametrize(("tool", "args", "element", "expected"), [
    ("browser_click", {"selector": "a"}, {"role": "link", "name": "More information..."}, "Clicar no link “More information...”"),
    ("browser_click", {"selector": "#go"}, {"role": "button", "name": "Enviar"}, "Clicar no botão “Enviar”"),
    ("browser_click", {"selector": "a"}, None, "Clicar no primeiro link da página"),
    ("browser_click", {"selector": "button"}, {"role": "button", "name": ""}, "Clicar no primeiro botão da página"),
    ("browser_click", {"selector": "text=More information"}, None, "Clicar no elemento com o texto “More information”"),
    ("browser_click", {"selector": "#nav > div:nth-child(2) .x"}, None, "Clicar em um elemento da página"),
    ("browser_fill", {"selector": "#email", "value": "x"}, {"role": "field", "name": "E-mail"}, "Preencher o campo “E-mail”"),
    ("browser_fill", {"selector": "input", "value": "x"}, None, "Preencher o primeiro campo da página"),
    ("browser_fill", {"selector": "#q", "value": "x"}, None, "Preencher o campo “q”"),
    ("browser_fill", {"selector": "#senha", "value": "x"}, None, "Preencher o campo “senha”"),
    ("browser_click", {"selector": "button#comprar"}, None, "Clicar no botão “comprar”"),
    ("browser_fill", {"selector": "input[name=email]", "value": "x"}, None, "Preencher o campo “email”"),
    ("browser_fill", {"selector": "form .row > input"}, None, "Preencher um campo da página"),
])
def test_descriptions_read_as_actions_on_named_things(tool, args, element, expected):
    assert _describe_tool_action(tool, args, element) == expected


@pytest.mark.parametrize("selector", ["a", "#nav > div:nth-child(2) .x", "div.card:nth-of-type(3) > a[href]"])
def test_a_raw_selector_never_reaches_the_person(selector):
    description = _describe_tool_action("browser_click", {"selector": selector}, None)
    assert f"'{selector}'" not in description
    assert len(selector) < 3 or selector not in description


def test_a_typed_value_is_never_shown():
    assert "hunter2" not in _describe_tool_action("browser_fill", {"selector": "#pw", "value": "hunter2"},
                                                  {"role": "field", "name": "Senha"})
