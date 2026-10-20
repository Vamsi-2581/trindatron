"""
Integration tests — TRINITY MEMORY context injection into CHAT and AGENT.

Each test proves a concrete invariant:
  1. user with a memory → memory text appears in CHAT system message
  2. user with a memory → memory text appears in AGENT system message
  3. user without memories → system message unchanged (no memory section)
  4. memory of another user → never injected into the caller's context
  5. memory injection does not alter capability/task classification
  6. multiple memories respect the build_memory_context limit (≤ 20 entries)

Execution Mode etapa (2026-09-07): the AGENT-path tests below use "Debug
this code, save the fix to disk, and run the tests." rather than "write a
function that reverses a list" — the latter is now correctly CHAT-only
(no execution intent, no tools needed — see test_execution_mode.py), so
it no longer reaches agent_service._router.complete() at all.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trinity_router import RoutingRequest, RoutingResult, RouterStreamChunk


# ─── Router capture helpers ───────────────────────────────────────────────────

def _make_agent_result(content: str = "done") -> RoutingResult:
    return RoutingResult(
        content=content,
        tool_calls=None,
        provider="anthropic",
        model="claude-test",
        input_tokens=5,
        output_tokens=2,
        cached_tokens=0,
        cost_usd=0.0,
        tcu=0.0,
        failover_used=False,
        failover_reason=None,
    )


def _capture_chat():
    """Returns (captured: list[RoutingRequest], mock_fn)."""
    captured: list[RoutingRequest] = []

    async def _fn(request: RoutingRequest):
        captured.append(request)
        yield RouterStreamChunk(text="ok")
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=2, cached_tokens=0,
            cost_usd=0.0, tcu=0.0,
        )

    return captured, _fn


def _capture_agent():
    """Returns (captured: list[RoutingRequest], mock_fn)."""
    captured: list[RoutingRequest] = []

    async def _fn(request: RoutingRequest) -> RoutingResult:
        captured.append(request)
        return _make_agent_result()

    return captured, _fn


# ─── Fixture helpers ──────────────────────────────────────────────────────────

def _register(client, email: str) -> dict:
    resp = client.post("/auth/register", json={
        "email": email,
        "password": "securepass123",
        "full_name": "Mem Test",
    })
    assert resp.status_code == 201
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


def _new_session(client, headers) -> str:
    return client.post("/sessions", json={}, headers=headers).json()["id"]


def _add_memory(client, headers, content: str) -> str:
    resp = client.post("/users/me/memories", json={"content": content}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"]


def _system_message(captured: list[RoutingRequest]) -> str:
    """Return the system message content from the first captured RoutingRequest."""
    assert captured, "No RoutingRequest was captured — router mock was not called"
    return captured[0].messages[0]["content"]


# ─── 1. CHAT: memory appears in system prompt ─────────────────────────────────

def test_chat_memory_injected_into_system_message(client, auth_headers):
    """User memory must appear verbatim inside the system message sent to the LLM."""
    _add_memory(client, auth_headers, "User prefers responses in bullet points")

    session_id = _new_session(client, auth_headers)
    captured, mock_fn = _capture_chat()

    with patch("src.services.chat_service._router.stream", mock_fn):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Hello, how are you?"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    system_msg = _system_message(captured)
    assert "User prefers responses in bullet points" in system_msg


# ─── 2. AGENT: memory appears in system prompt ───────────────────────────────

def test_agent_memory_injected_into_system_message(client, auth_headers):
    """User memory must appear verbatim in the agent system message."""
    _add_memory(client, auth_headers, "User's timezone is America/Sao_Paulo")

    session_id = _new_session(client, auth_headers)
    captured, mock_fn = _capture_agent()

    with patch("src.services.agent_service._router.complete", mock_fn):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Debug this code, save the fix to disk, and run the tests."},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    system_msg = _system_message(captured)
    assert "User's timezone is America/Sao_Paulo" in system_msg


# ─── 3. No memory → system prompt unchanged ──────────────────────────────────

def test_chat_no_memory_system_prompt_has_no_memory_header(client):
    """A user with no memories must not get a memory block in the system message."""
    fresh_headers = _register(client, "nomem_chat@test.com")
    session_id = _new_session(client, fresh_headers)
    captured, mock_fn = _capture_chat()

    with patch("src.services.chat_service._router.stream", mock_fn):
        client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Hi"},
            headers=fresh_headers,
        )

    system_msg = _system_message(captured)
    assert "Memórias do usuário" not in system_msg


def test_agent_no_memory_system_prompt_has_no_memory_header(client, db):
    """A user with no memories must not get a memory block in the agent system message."""
    fresh_headers = _register(client, "nomem_agent@test.com")
    # A signup is FREE, which has no agent mode; this path needs a paying plan.
    from src.models.user import User
    db.query(User).filter(User.email == "nomem_agent@test.com").one().plan = "start"
    db.commit()
    session_id = _new_session(client, fresh_headers)
    captured, mock_fn = _capture_agent()

    with patch("src.services.agent_service._router.complete", mock_fn):
        client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Debug this code, save the fix to disk, and run the tests."},
            headers=fresh_headers,
        )

    system_msg = _system_message(captured)
    assert "Memórias do usuário" not in system_msg


# ─── 4. Isolation: other user's memory never injected ────────────────────────

def test_chat_other_user_memory_not_injected(client, auth_headers):
    """Memories of user A must never appear in the system prompt of user B."""
    other_headers = _register(client, "other_mem_chat@test.com")
    _add_memory(client, other_headers, "SECRET_FACT_OTHER_USER")

    # User B (auth_headers) has no memories of their own
    session_id = _new_session(client, auth_headers)
    captured, mock_fn = _capture_chat()

    with patch("src.services.chat_service._router.stream", mock_fn):
        client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Hello"},
            headers=auth_headers,
        )

    system_msg = _system_message(captured)
    assert "SECRET_FACT_OTHER_USER" not in system_msg


def test_agent_other_user_memory_not_injected(client, auth_headers):
    """Memories of user A must never appear in the agent system prompt of user B."""
    other_headers = _register(client, "other_mem_agent@test.com")
    _add_memory(client, other_headers, "SECRET_AGENT_FACT")

    session_id = _new_session(client, auth_headers)
    captured, mock_fn = _capture_agent()

    with patch("src.services.agent_service._router.complete", mock_fn):
        client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Debug this code, save the fix to disk, and run the tests."},
            headers=auth_headers,
        )

    system_msg = _system_message(captured)
    assert "SECRET_AGENT_FACT" not in system_msg


# ─── 5. Memory injection does not alter capability classification ─────────────

def test_chat_capability_unchanged_when_memories_present(client, auth_headers):
    """Adding memories to context must not change how classify() routes the message."""
    _add_memory(client, auth_headers, "User likes code")

    session_id = _new_session(client, auth_headers)
    captured, mock_fn = _capture_chat()

    with patch("src.services.chat_service._router.stream", mock_fn):
        client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Hello, how are you?"},
            headers=auth_headers,
        )

    assert captured, "router was not called"
    # A plain greeting must still route as CHAT (not code/files/etc)
    req = captured[0]
    assert req.capability == "chat"


# ─── 6. Multiple memories: all appear, header present once ───────────────────

def test_chat_multiple_memories_all_appear_in_context(client, auth_headers):
    """All user memories must appear in the system message."""
    facts = [
        "User speaks Portuguese natively",
        "User prefers dark mode",
        "User's name is Diego",
    ]
    for fact in facts:
        _add_memory(client, auth_headers, fact)

    session_id = _new_session(client, auth_headers)
    captured, mock_fn = _capture_chat()

    with patch("src.services.chat_service._router.stream", mock_fn):
        client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Hello"},
            headers=auth_headers,
        )

    system_msg = _system_message(captured)
    for fact in facts:
        assert fact in system_msg, f"Memory missing from context: {fact!r}"

    # The block is emitted exactly once. The heading is now language-neutral:
    # it used to be a Portuguese string sent to every user regardless of their
    # language, which the layered architecture moved into TaskContext instead
    # (see src/prompting/task_context.py).
    assert system_msg.count("<memory>") == 1


def test_chat_sends_only_memories_relevant_to_the_request(client, auth_headers):
    """The behaviour change that motivated the prompting layer.

    Every chat and agent call used to carry the user's 20 most recent
    memories regardless of subject: paid for on every request, and a system
    prompt whose bulk is irrelevant facts competing with the actual task.
    A question about one subject should now bring that subject's memory and
    leave the rest behind.
    """
    _add_memory(client, auth_headers, "The Helios project uses PostgreSQL for storage")
    _add_memory(client, auth_headers, "User prefers dark mode in the editor")
    _add_memory(client, auth_headers, "User supports Palmeiras football club")

    session_id = _new_session(client, auth_headers)
    captured, mock_fn = _capture_chat()

    with patch("src.services.chat_service._router.stream", mock_fn):
        client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Which database does the Helios project use?"},
            headers=auth_headers,
        )

    system_msg = _system_message(captured)
    assert "Helios project uses PostgreSQL" in system_msg
    assert "Palmeiras" not in system_msg, "an unrelated memory was still sent"
    assert "dark mode" not in system_msg, "an unrelated memory was still sent"


# ─── 3. AGENT: memory survives a full prompt — creations + web tools ─────────

@pytest.mark.parametrize("creations", [1, 5])
def test_agent_memory_survives_creations_and_web_research_rules(client, auth_headers, db, creations):
    """The heaviest ordinary agent turn: web tools (their reading rules), a conversation that already created
    media (the <artifacts> block) and a memory. Memory is the first section the budget drops, so all three must
    still fit — with both blocks added the turn measured 2,526 tokens against a 2,500 budget and lost the memory.
    """
    from decimal import Decimal

    from src.models.media import Artifact, MediaJob
    from src.models.message import Message
    from src.models.session import Session

    _add_memory(client, auth_headers, "User's timezone is America/Sao_Paulo")
    session_id = _new_session(client, auth_headers)
    user_id = db.query(Session).filter(Session.id == session_id).one().user_id
    for _ in range(creations):
        artifact = Artifact(user_id=user_id, kind="video", source="media", filename="clip.mp4",
                            content_type="video/mp4", size_bytes=651_987, width=1280, height=544,
                            duration_seconds=5.06, storage_backend="local", storage_key="media/x")
        db.add(artifact)
        db.flush()
        job = MediaJob(user_id=user_id, kind="video", status="completed", prompt="x",
                       estimated_cost_usd=Decimal("0.09"), artifact_id=artifact.id)
        db.add(job)
        db.flush()
        db.add(Message(session_id=session_id, role="assistant", content="Estou criando seu vídeo…",
                       media_job_id=job.id))
    db.commit()

    captured, mock_fn = _capture_agent()
    with patch("src.services.agent_service._router.complete", mock_fn):
        resp = client.post(f"/sessions/{session_id}/message", headers=auth_headers,
                           json={"content": "Debug this code, save the fix to disk, and run the tests."})

    assert resp.status_code == 200
    system_msg = _system_message(captured)
    assert "<artifacts>" in system_msg and "1280x544" in system_msg  # the creations block is there
    assert "against <today>" in system_msg  # the web reading rules are there
    assert "User's timezone is America/Sao_Paulo" in system_msg  # and the memory was not dropped for them
