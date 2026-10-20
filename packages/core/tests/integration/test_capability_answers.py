"""What TRINDATRON says about itself, end to end through the conversation route.

Questions about a capability are answered from the capability matrix, whatever an older conversation said and
without any model; a request to create goes to the media path; who TRINDATRON is and how current it is are answered
by the model under the system policy. The model router is mocked, so every check is deterministic.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from trinity_router import RouterStreamChunk

from src.models.message import Message
from src.models.user import User


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


def _answer(body: str) -> str:
    events = _events(body)
    return "".join(e.get("content", "") for e in events if e["type"] == "text") + \
        "".join(e.get("answer", "") for e in events if e["type"] == "done")


def _router(calls: list, text: str = "Sou o TRINDATRON. " * 6):
    async def _stream(request):
        calls.append(request)
        yield RouterStreamChunk(text=text)
        yield RouterStreamChunk(is_final=True, provider="p", model="m", input_tokens=10, output_tokens=10,
                                cached_tokens=0, cost_usd=0.0001, tcu=0.01)
    return _stream


def _matrix(**statuses: str) -> dict[str, dict]:
    return {name: {"available": status == "available", "status": status} for name, status in statuses.items()}


def _set_plan(db, user_id: str, plan: str) -> None:
    user = db.query(User).filter(User.id == user_id).one()
    user.plan = plan
    db.commit()


def _session(client, headers, db=None, history: list[tuple[str, str]] = ()) -> str:
    session_id = client.post("/sessions", json={}, headers=headers).json()["id"]
    if history:
        start = datetime.now(timezone.utc) - timedelta(days=1)
        for offset, (role, content) in enumerate(history):
            db.add(Message(session_id=session_id, role=role, content=content, created_at=start + timedelta(minutes=offset)))
        db.commit()
    return session_id


def _ask(client, headers, session_id: str, content: str, matrix: dict, calls: list):
    with patch("src.services.product_answers.current_matrix", return_value=matrix), \
            patch("src.services.chat_service._router.stream", _router(calls)):
        return client.post(f"/sessions/{session_id}/message", json={"content": content}, headers=headers)


# An old conversation whose earlier replies claimed the opposite of the matrix.
CONTAMINATED = [
    ("user", "Então gere um vídeo pra mim"),
    ("assistant", "Não consigo criar imagens ou vídeos. Você pode usar ferramentas como DALL‑E ou Runway."),
    ("user", "Você cria vídeos?"),
    ("assistant", "Não consigo criar vídeos, procure no YouTube."),
]


@pytest.mark.parametrize("question,matrix,phrase", [
    ("Você consegue criar imagens?", _matrix(image_generation="available"), "Criar imagens está disponível para você agora"),
    ("Você consegue criar imagens?", _matrix(image_generation="plan"), "não está incluído no seu plano atual"),
    ("Você consegue criar imagens?", _matrix(image_generation="unavailable"), "temporariamente indisponível"),
    ("Você consegue criar vídeos?", _matrix(video_generation="plan"), "Criar vídeos não está incluído no seu plano atual"),
    ("Você consegue criar vídeos?", _matrix(video_generation="unavailable"), "temporariamente indisponível"),
    ("Você consegue criar vídeos?", _matrix(video_generation="not_integrated"), "ainda não está integrado"),
    ("Nem. Vídeos?", _matrix(video_generation="plan"), "Criar vídeos não está incluído no seu plano atual"),
])
@pytest.mark.parametrize("conversation", ["new", "old"])
def test_a_capability_question_is_answered_from_the_matrix_without_a_model(
    client, auth_headers, db, question, matrix, phrase, conversation,
):
    history = CONTAMINATED if conversation == "old" else ()
    session_id = _session(client, auth_headers, db, history)
    calls: list = []
    resp = _ask(client, auth_headers, session_id, question, matrix, calls)

    assert resp.status_code == 200
    assert calls == [], "a capability question never reaches a model"
    answer = _answer(resp.text)
    assert phrase in answer
    for refusal in ("não consigo", "não posso", "YouTube", "DALL", "Runway"):
        assert refusal not in answer
    # The answer is part of the conversation, like any reply, and costs nothing.
    stored = [m["content"] for m in client.get(f"/sessions/{session_id}", headers=auth_headers).json()["messages"]]
    assert stored[-2:] == [question, answer]
    assert client.get("/users/me/usage", headers=auth_headers).json()["daily"]["used"] == 0


def test_a_capability_question_never_starts_a_media_job(client, auth_headers, db):
    session_id = _session(client, auth_headers)
    with patch("src.services.chat_media.start_from_chat") as start:
        resp = _ask(client, auth_headers, session_id, "consegue criar imagens?", _matrix(image_generation="available"), [])
    assert resp.status_code == 200
    start.assert_not_called()


def test_asking_about_the_web_on_a_plan_without_tools_is_answered_not_refused(client, auth_headers, registered_user, db):
    """"pesquisar na internet" looks like execution intent; on FREE an agent run would be a 403."""
    _set_plan(db, registered_user["user_id"], "free")
    session_id = _session(client, auth_headers)
    calls: list = []
    resp = _ask(client, auth_headers, session_id, "Você consegue pesquisar na Internet?", _matrix(web_search="plan"), calls)

    assert resp.status_code == 200
    assert calls == []
    assert "Pesquisar na web não está incluído no seu plano atual" in _answer(resp.text)


@pytest.mark.parametrize("request_text,kind", [("Crie a imagem dele", "image"), ("gera o vídeo", "video")])
def test_a_request_with_an_article_goes_to_the_media_path(client, auth_headers, request_text, kind):
    session_id = _session(client, auth_headers)
    calls: list = []

    async def _start(db, user, content, media_kind, session_id=None, language=None, confirmed=False):
        return ("started", None)

    with patch("src.services.chat_media.start_from_chat", side_effect=_start) as start, \
            patch("src.services.chat_service._router.stream", _router(calls)):
        resp = client.post(f"/sessions/{session_id}/message", json={"content": request_text}, headers=auth_headers)

    assert resp.status_code == 200
    assert calls == [], "a creation is never answered by a model in words"
    assert start.call_args.args[3] == kind


def _system_prompt(call) -> str:
    return call.messages[0]["content"]


@pytest.mark.parametrize("question", ["Quem é você?", "Qual é sua data de conhecimento?"])
def test_identity_and_currency_are_answered_by_the_model_under_the_policy(client, auth_headers, question):
    """Not a fixed reply: the model answers, told who TRINDATRON is, what it can do in this request, and that a
    training cutoff is never stated."""
    session_id = _session(client, auth_headers)
    calls: list = []
    with patch("src.services.chat_service._router.stream", _router(calls)):
        resp = client.post(f"/sessions/{session_id}/message", json={"content": question}, headers=auth_headers)

    assert resp.status_code == 200
    assert len(calls) == 1
    system = _system_prompt(calls[0])
    assert '<trindatron_policy version="2">' in system
    assert "You are TRINDATRON" in system and "Never state a training cutoff" in system
    assert "<capabilities>" in system and "<today>" in system
    assert "Your own knowledge ends before this date" not in system


def test_you_are_chatgpt_is_still_answered_without_a_model(client, auth_headers):
    session_id = _session(client, auth_headers)
    calls: list = []
    with patch("src.services.chat_service._router.stream", _router(calls)):
        resp = client.post(f"/sessions/{session_id}/message", json={"content": "Você é ChatGPT?"}, headers=auth_headers)
    assert calls == []
    assert "TRINDATRON" in _answer(resp.text)


@pytest.mark.parametrize("status,phrase", [
    ("plan", "Pesquisar na web não está incluído no seu plano atual"),
    ("unavailable", "Pesquisar na web faz parte do TRINDATRON, mas está temporariamente indisponível"),
    ("not_integrated", "Pesquisar na web ainda não está integrado"),
])
def test_news_without_web_search_is_answered_with_the_reason_not_by_a_model(
    client, auth_headers, registered_user, db, status, phrase,
):
    """"Pesquise as notícias de hoje" where web search is not available. On the real Core the model, told the same
    reason, still answered "não consigo", in English once, and named the internal capability."""
    _set_plan(db, registered_user["user_id"], "free")
    session_id = _session(client, auth_headers)
    calls: list = []
    resp = _ask(client, auth_headers, session_id, "Pesquise as notícias de hoje.", _matrix(web_search=status), calls)

    assert resp.status_code == 200
    assert calls == []
    answer = _answer(resp.text)
    assert phrase in answer and "não podem ser verificadas nesta conversa" in answer
    assert "não consigo" not in answer and "web_search" not in answer


def test_a_question_about_the_present_that_a_model_can_still_help_with_reaches_the_model(client, auth_headers, registered_user, db):
    """Not every question about now is a search: the model answers under the currency rules, told that web
    search is not in the plan."""
    _set_plan(db, registered_user["user_id"], "free")
    session_id = _session(client, auth_headers)
    calls: list = []
    resp = _ask(client, auth_headers, session_id, "Quem é o presidente atual dos Estados Unidos?", _matrix(web_search="plan"), calls)

    assert resp.status_code == 200
    assert len(calls) == 1
    system = _system_prompt(calls[0])
    assert "web_search (not included in this person's current plan)" in system
    assert "not a limit of TRINDATRON" in system and "never as \"I cannot\"" in system


def test_news_where_web_search_exists_goes_to_the_tool_loop():
    from src.capability.classifier import classify
    from src.capability.pipelines import TaskType, infer_execution_mode

    text = "Pesquise as notícias de hoje."
    mode = infer_execution_mode(capability=classify(text), messages=[{"role": "user", "content": text}],
                                agent_available=True)
    assert mode is TaskType.AGENT
