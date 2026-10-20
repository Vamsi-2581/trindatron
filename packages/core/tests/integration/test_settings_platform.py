"""Settings, integrations, sessions, data export and scheduled tasks, through the real routes.

Only the model router behind a scheduled research job is scripted; plans,
preferences, the agent tool list, refresh-token families, the schedule poll
cycle, the durable worker and usage accounting are the real machinery.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from trinity_router import RoutingResult

from src.config import settings
from src.execution.fake_provider import FakeExecutionProvider
from src.models.agent_definition import AgentDefinition
from src.models.agent_schedule import AgentSchedule
from src.models.memory import Memory
from src.models.session import Session
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import agent_schedule_service, execution_service, spend_guard

PROVIDERS = re.compile(r"openai|anthropic|claude|gemini|google|mistral|groq|xai|grok|litellm|deepseek|"
                       r"model_used|provider_cost|cost_usd|\btcu\b", re.I)


def _plan(db, user_id: str, plan: str) -> None:
    db.query(User).filter(User.id == user_id).update({"plan": plan})
    db.commit()


# ── Preferences ─────────────────────────────────────────────────────────────

def test_preferences_start_at_safe_defaults_and_persist(client, auth_headers):
    first = client.get("/users/me/preferences", headers=auth_headers).json()
    assert first == {"language": "auto", "response_style": "balanced", "send_on_enter": True,
                     "voice_auto_play": False, "memory_enabled": True, "disabled_plugins": []}
    changed = client.patch("/users/me/preferences", headers=auth_headers,
                           json={"language": "es", "response_style": "concise", "send_on_enter": False,
                                 "voice_auto_play": True})
    assert changed.status_code == 200 and changed.json()["language"] == "es"
    stored = client.get("/users/me/preferences", headers=auth_headers).json()
    assert stored["send_on_enter"] is False and stored["voice_auto_play"] is True


# theme and voice_input_language have nothing that honours them yet (schemas/preferences.py), so they are refused.
@pytest.mark.parametrize("body", [{"theme": "dark"}, {"voice_input_language": "pt"}, {"language": "klingon"},
                                  {"favorite_model": "anything"}, {"disabled_plugins": ["teleport"]}])
def test_preferences_refuse_values_and_keys_that_do_nothing(client, auth_headers, body):
    assert client.patch("/users/me/preferences", headers=auth_headers, json=body).status_code == 422


def test_preferences_reach_the_prompt_and_memory_off_keeps_memories_out(client, db, auth_headers, registered_user):
    from src.prompting.context_builders import build_relevant_memory, build_task_context

    user_id = registered_user["user_id"]
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    db.add(Memory(user_id=user_id, content="Prefiro relatórios em tabelas"))
    db.commit()
    client.patch("/users/me/preferences", headers=auth_headers, json={"language": "en", "response_style": "detailed"})

    session = db.query(Session).filter(Session.id == session_id).one()
    rendered = build_task_context(db, session).render()
    assert "Respond in English" in rendered and "thorough answers" in rendered
    assert "tabelas" in build_relevant_memory(db, user_id, "relatórios").render()

    client.patch("/users/me/preferences", headers=auth_headers, json={"memory_enabled": False})
    assert build_relevant_memory(db, user_id, "relatórios").render() == ""
    assert db.query(Memory).filter(Memory.user_id == user_id).count() == 1  # kept, just not used


def test_automatic_language_is_the_device_and_a_chosen_language_beats_it(client, db, auth_headers, registered_user):
    """"Automatic" follows the language the client sends (the phone's own), never the words of the message."""
    from src.prompting.context_builders import build_task_context
    from src.services import preferences_service

    user_id = registered_user["user_id"]
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    session = db.query(Session).filter(Session.id == session_id).one()

    # Automatic, phone in English: English, even for a Portuguese message.
    assert "Respond in English" in build_task_context(db, session, client_locale="en-US").render()
    assert preferences_service.answer_language(db, user_id, "en-US") == "en"

    # Chosen in Settings: the choice wins over the phone.
    client.patch("/users/me/preferences", headers=auth_headers, json={"language": "es"})
    db.expire_all()
    assert "Respond in Spanish" in build_task_context(db, session, client_locale="en-US").render()
    assert preferences_service.answer_language(db, user_id, "en-US") == "es"

    # Back to automatic and an older client that sends nothing: mirror the person, as before.
    client.patch("/users/me/preferences", headers=auth_headers, json={"language": "auto"})
    db.expire_all()
    assert "language the person is writing in" in build_task_context(db, session).render()
    # Nothing known: "" — each model-free reply then reads the text with its own hints, as it always did.
    assert preferences_service.answer_language(db, user_id, None) == ""


def test_the_message_endpoint_answers_in_the_client_locale(client, auth_headers):
    from src.prompting.identity_guard import detect_language

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    # A capability question is answered by the product itself, so no model runs and the language is the device's.
    response = client.post(f"/sessions/{session_id}/message", headers=auth_headers,
                           json={"content": "Você consegue criar imagens?", "locale": "en-US"})
    assert response.status_code == 200
    text = "".join(re.findall(r'"type": "text", "content": "([^"]*)"', response.text))
    assert text, response.text
    assert detect_language(text) == "en", text


# ── Plugins, connectors, extensions ─────────────────────────────────────────

def test_integrations_report_real_state_and_a_switched_off_plugin_leaves_the_agent(client, db, auth_headers,
                                                                                     registered_user):
    from src.services.agent_service import _prepare_agent_context

    view = client.get("/users/me/integrations", headers=auth_headers)
    body = view.json()
    assert [p["id"] for p in body["plugins"]] == ["web_search", "browser", "files", "code_execution", "computer"]
    browser = next(p for p in body["plugins"] if p["id"] == "browser")
    assert set(browser["approval_required"]) == {"browser_click", "browser_fill"}
    assert all(c["status"] == "not_available" and c["connected"] is False for c in body["connectors"])
    assert body["extensions"] == []
    assert not PROVIDERS.search(view.text)

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    session = db.query(Session).filter(Session.id == session_id).one()
    _, tools_before = _prepare_agent_context(db, session, "abra um site")
    assert "browser_navigate" in {t["function"]["name"] for t in tools_before}

    off = client.patch("/users/me/integrations/plugins/browser", headers=auth_headers, json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["status"] in ("disabled", "plan",
                                                                                                  "unavailable")
    _, tools_after = _prepare_agent_context(db, session, "abra um site")
    names = {t["function"]["name"] for t in tools_after}
    assert not names & {"browser_navigate", "browser_get_text", "browser_screenshot", "browser_click", "browser_fill"}
    assert "web_search" in names

    assert client.patch("/users/me/integrations/plugins/browser", headers=auth_headers,
                        json={"enabled": True}).json()["enabled"] is True
    assert client.patch("/users/me/integrations/plugins/teleport", headers=auth_headers,
                        json={"enabled": True}).status_code == 404


# ── Sessions and devices ────────────────────────────────────────────────────

def test_a_person_sees_their_devices_signs_one_out_and_then_all(client, auth_headers, registered_user):
    for _ in range(2):  # two phones signed in
        signed_in = client.post("/auth/login", json={"email": registered_user["email"], "password": "securepass123",
                                                     "client": "mobile"})
        assert signed_in.status_code == 200
    sessions = client.get("/auth/sessions", headers=auth_headers).json()["sessions"]
    assert len(sessions) == 2
    assert not any("token" in key for session in sessions for key in session)
    other = next(s for s in sessions if s["client"] == "mobile")

    assert client.delete(f"/auth/sessions/{other['id']}", headers=auth_headers).status_code == 204
    assert client.delete(f"/auth/sessions/{other['id']}", headers=auth_headers).status_code == 404
    assert len(client.get("/auth/sessions", headers=auth_headers).json()["sessions"]) == 1

    assert client.post("/auth/sessions/revoke-all", headers=auth_headers).status_code == 204
    assert client.get("/auth/sessions", headers=auth_headers).json()["sessions"] == []


def test_the_app_sees_its_own_sign_in_as_this_device_without_a_cookie(client, registered_user):
    """The app keeps its refresh token in secure storage and sends no cookie; its access token says which sign-in it
    is. On a physical phone every row used to come back unmarked, the phone's own included, with a sign-out menu."""
    def sign_in():
        response = client.post("/auth/login", json={"email": registered_user["email"], "password": "securepass123",
                                                    "client": "mobile"})
        assert response.status_code == 200
        return response.json()["access_token"]

    phone, other_phone = sign_in(), sign_in()
    client.cookies.clear()  # nothing but the bearer, as the app sends it

    sessions = client.get("/auth/sessions", headers={"Authorization": f"Bearer {phone}"}).json()["sessions"]
    assert sum(session["current"] for session in sessions) == 1
    seen_from_the_other = client.get("/auth/sessions",
                                     headers={"Authorization": f"Bearer {other_phone}"}).json()["sessions"]
    assert sum(session["current"] for session in seen_from_the_other) == 1
    # Each phone is "this device" to itself, and only to itself.
    mine = next(session["id"] for session in sessions if session["current"])
    theirs = next(session["id"] for session in seen_from_the_other if session["current"])
    assert mine != theirs

    # An access token from before this change carries no session id: nothing is marked, nothing breaks.
    from src.services.auth_service import create_access_token
    legacy = create_access_token(registered_user["user_id"], minutes=5)
    legacy_view = client.get("/auth/sessions", headers={"Authorization": f"Bearer {legacy}"})
    assert legacy_view.status_code == 200
    assert not any(session["current"] for session in legacy_view.json()["sessions"])


# ── Your data ───────────────────────────────────────────────────────────────

def test_export_contains_what_the_person_created_and_nothing_about_how_it_was_served(client, db, auth_headers,
                                                                                       registered_user):
    from src.models.message import Message

    user_id = registered_user["user_id"]
    session_id = client.post("/sessions", json={"title": "Plano de viagem"}, headers=auth_headers).json()["id"]
    db.add(Message(session_id=session_id, role="user", content="Quero ir a Lisboa"))
    db.add(Message(session_id=session_id, role="assistant", content="Ótima escolha", model_used="claude-sonnet"))
    db.add(Memory(user_id=user_id, content="Mora em Cuiabá"))
    db.commit()

    exported = client.get("/users/me/export", headers=auth_headers)
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    data = exported.json()
    assert data["profile"]["email"] == registered_user["email"]
    assert data["memories"][0]["content"] == "Mora em Cuiabá"
    conversation = next(c for c in data["conversations"] if c["id"] == session_id)
    assert [m["content"] for m in conversation["messages"]] == ["Quero ir a Lisboa", "Ótima escolha"]
    assert not PROVIDERS.search(exported.text)

    cleared = client.delete("/users/me/memories", headers=auth_headers)
    assert cleared.json() == {"deleted": 1}
    assert db.query(Memory).filter(Memory.user_id == user_id).count() == 0


# ── Scheduled tasks ─────────────────────────────────────────────────────────

class _FakeRouter:
    def __init__(self) -> None:
        self.calls: list = []

    async def complete(self, request):
        self.calls.append(request)
        return RoutingResult(content="Resumo do dia: três prioridades.", tool_calls=None, provider="anthropic",
                             model="claude-sonnet", input_tokens=10, output_tokens=20, cached_tokens=0,
                             cost_usd=0.001, tcu=0.1, failover_used=False, failover_reason=None)


@pytest.fixture
def scheduling(monkeypatch, tmp_path, execution_session_override):
    from src.execution import model_router_provider

    fake = _FakeRouter()
    monkeypatch.setattr(model_router_provider, "_router", fake)
    monkeypatch.setattr(execution_service, "build_default_provider", lambda: FakeExecutionProvider(tmp_path))
    return fake


_DAILY = {"title": "Resumo diário", "instruction": "Faça um resumo das minhas prioridades do dia.",
          "recurrence": {"kind": "daily", "time": "09:00", "timezone": "America/Cuiaba"}}


def test_free_is_told_scheduled_tasks_are_not_in_its_plan(client, db, auth_headers, registered_user):
    _plan(db, registered_user["user_id"], "free")
    refused = client.post("/scheduled-tasks", headers=auth_headers, json=_DAILY)
    assert refused.status_code == 403 and refused.json()["detail"]["error"] == "plan_not_included"


@pytest.mark.parametrize("recurrence", [{"kind": "yearly"}, {"kind": "daily", "time": "25:00"},
                                        {"kind": "weekly", "time": "09:00", "weekday": 9}])
def test_a_recurrence_the_engine_cannot_run_is_refused(client, auth_headers, recurrence):
    assert client.post("/scheduled-tasks", headers=auth_headers,
                       json={**_DAILY, "recurrence": recurrence}).status_code == 422


async def test_a_scheduled_task_is_created_fired_by_the_existing_scheduler_and_runs(client, db, auth_headers,
                                                                                    registered_user, scheduling,
                                                                                    run_worker_until_terminal):
    created = client.post("/scheduled-tasks", headers=auth_headers, json=_DAILY)
    assert created.status_code == 201
    task = created.json()
    assert task["recurrence"] == {"kind": "daily", "timezone": "America/Cuiaba", "time": "09:00"}
    next_run = datetime.fromisoformat(task["next_run_at"])
    assert next_run > datetime.now(timezone.utc)
    assert next_run.astimezone(timezone(timedelta(hours=-4))).strftime("%H:%M") == "09:00"

    agent = db.query(AgentDefinition).filter(AgentDefinition.user_id == registered_user["user_id"]).one()
    assert agent.is_archived is True  # never listed among the person's own agents

    schedule = db.query(AgentSchedule).filter(AgentSchedule.id == task["id"]).one()
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired == 1
    db.refresh(schedule)
    assert schedule.last_decision == "acted" and schedule.interval_seconds == 86400

    job = await run_worker_until_terminal(db, schedule.last_run_job_id, timeout=10.0)
    assert job.status == "completed" and job.job_type == "research"
    assert "Faça um resumo das minhas prioridades" in scheduling.calls[0].messages[-1]["content"]

    listed = client.get("/scheduled-tasks", headers=auth_headers)
    view = listed.json()["tasks"][0]
    assert view["last_run"]["status"] == "completed" and "três prioridades" in view["last_run"]["excerpt"]
    assert not PROVIDERS.search(listed.text)

    # The same one-active-job rule the scheduler uses applies to "run now".
    run = client.post(f"/scheduled-tasks/{task['id']}/run", headers=auth_headers)
    assert run.status_code == 202
    again = client.post(f"/scheduled-tasks/{task['id']}/run", headers=auth_headers)
    assert again.status_code == 409 and again.json()["detail"]["error"] == "job_in_progress"


def test_the_limit_counts_tasks_that_actually_run(client, auth_headers):
    from src.services.scheduled_task_service import MAX_ACTIVE_TASKS

    for number in range(MAX_ACTIVE_TASKS):
        created = client.post("/scheduled-tasks", headers=auth_headers, json={**_DAILY, "title": f"Tarefa {number}"})
        assert created.status_code == 201

    refused = client.post("/scheduled-tasks", headers=auth_headers, json={**_DAILY, "title": "Uma a mais"})
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "limit_reached"

    # Pausing one frees the slot: what is capped is what the scheduler will fire, not what is kept.
    paused = client.get("/scheduled-tasks", headers=auth_headers).json()["tasks"][0]["id"]
    assert client.patch(f"/scheduled-tasks/{paused}", headers=auth_headers, json={"is_active": False}).status_code == 200
    assert client.post("/scheduled-tasks", headers=auth_headers,
                       json={**_DAILY, "title": "Depois da pausa"}).status_code == 201


def test_a_task_can_be_paused_edited_and_deleted_and_nobody_else_can_touch_it(client, db, auth_headers,
                                                                              db2_user_factory):
    task = client.post("/scheduled-tasks", headers=auth_headers, json=_DAILY).json()
    paused = client.patch(f"/scheduled-tasks/{task['id']}", headers=auth_headers,
                          json={"is_active": False, "title": "Resumo da manhã",
                                "recurrence": {"kind": "weekly", "time": "08:30", "weekday": 0}})
    body = paused.json()
    assert body["is_active"] is False and body["next_run_at"] is None and body["title"] == "Resumo da manhã"
    assert body["recurrence"]["kind"] == "weekly"

    stranger = db2_user_factory("stranger@test.com")
    from src.services.auth_service import create_access_token

    other = {"Authorization": f"Bearer {create_access_token(stranger.id)}"}
    assert client.patch(f"/scheduled-tasks/{task['id']}", headers=other, json={"is_active": True}).status_code == 404
    assert client.delete(f"/scheduled-tasks/{task['id']}", headers=other).status_code == 404

    assert client.delete(f"/scheduled-tasks/{task['id']}", headers=auth_headers).status_code == 204
    assert client.get("/scheduled-tasks", headers=auth_headers).json()["tasks"] == []


# ── Real provider spend vs list price ───────────────────────────────────────

def test_a_free_provider_tier_is_recorded_as_zero_spend_while_tcu_stays_at_list_price(db, registered_user,
                                                                                      monkeypatch):
    from src.billing.provider_billing import billed_cost_usd
    from src.services.entitlement_service import finalize_usage

    user_id = registered_user["user_id"]

    def reserve() -> str:
        record = UsageRecord(user_id=user_id, model="pending", status="pending", tcu=Decimal("1"))
        db.add(record)
        db.commit()
        return record.id

    monkeypatch.setattr(settings, "groq_billing_tier", "free")
    assert billed_cost_usd("groq/openai/gpt-oss-20b", 0.0004) == 0.0
    assert billed_cost_usd("groq:stt:whisper-large-v3-turbo", 0.0001) == 0.0
    assert billed_cost_usd("gemini/gemini-2.5-flash-lite", 0.0004) == 0.0004

    groq = finalize_usage(db, reserve(), message_id=None, model="groq/openai/gpt-oss-20b", input_tokens=100,
                          output_tokens=50, cached_tokens=0, cost_usd=0.0004, tcu=0.04, duration_ms=10)
    paid = finalize_usage(db, reserve(), message_id=None, model="gemini/gemini-2.5-flash-lite", input_tokens=100,
                          output_tokens=50, cached_tokens=0, cost_usd=0.0006, tcu=0.06, duration_ms=10)
    assert groq.provider_cost_usd == Decimal("0") and float(groq.tcu) == pytest.approx(0.04)
    assert paid.provider_cost_usd is None

    now = datetime.now(timezone.utc)
    spend = spend_guard.spend_by_model(db, now - timedelta(hours=1), now + timedelta(minutes=1))
    assert spend["groq/openai/gpt-oss-20b"] == pytest.approx(0.0)
    assert spend["gemini/gemini-2.5-flash-lite"] == pytest.approx(0.0006)

    monkeypatch.setattr(settings, "groq_billing_tier", "developer")
    assert billed_cost_usd("groq/openai/gpt-oss-20b", 0.0004) == 0.0004


def test_my_subscriptions_list_only_own_and_no_processor_ids(client, auth_headers):
    resp = client.get("/users/me/subscriptions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"subscriptions": []}
    assert client.get("/users/me/subscriptions").status_code in (401, 403)
