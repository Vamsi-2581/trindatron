"""What chat and agent actually hand the model, built from a real (test) database.

The stored conversation must stay whole — the chat shows it — while the model context leaves out earlier replies
that stated capability availability, and the current <capabilities> block is still the one that answers it.
"""
from datetime import datetime, timedelta, timezone

from src.models.message import Message
from src.models.session import Session
from src.services import agent_service, chat_service
from src.services.auth_service import create_user

STORED = [
    ("user", "Então gere um vídeo pra mim, quantos segundos vc consegue criar"),
    ("assistant", "Não consigo criar imagens ou vídeos. Você pode usar ferramentas como DALL‑E ou Runway."),
    ("user", "Explique a fotossíntese em uma frase."),
    ("assistant", "Plantas transformam luz, água e CO₂ em glicose e oxigênio."),
    ("user", "Você cria vídeos?"),
    ("assistant", "Não consigo criar vídeos."),
]
CURRENT = "Você consegue criar vídeos para mim?"


def _conversation(db, email: str, stored=STORED) -> Session:
    user = create_user(db, email=email, password="validation-only-9x", full_name="History Context")
    session = Session(user_id=user.id, title="Conversa antiga", model_default="auto")
    db.add(session)
    db.commit()
    start = datetime.now(timezone.utc) - timedelta(days=1)
    for offset, (role, content) in enumerate(stored):
        db.add(Message(session_id=session.id, role=role, content=content, created_at=start + timedelta(minutes=offset)))
    db.commit()
    return session


def _assert_old_conversation_context(messages: list[dict]) -> None:
    assert messages[0]["role"] == "system"
    assert "<capabilities>" in messages[0]["content"]
    assert [(m["role"], m["content"]) for m in messages[1:]] == [
        ("user", "Explique a fotossíntese em uma frase."),
        ("assistant", "Plantas transformam luz, água e CO₂ em glicose e oxigênio."),
        ("user", CURRENT),
    ]


def test_chat_context_of_an_old_conversation(db):
    session = _conversation(db, "history-chat-old@example.com")

    _, messages = chat_service._prepare_chat_context(db, session, CURRENT)

    _assert_old_conversation_context(messages)
    # Nothing was deleted: the chat still shows every message, plus the one just sent.
    stored = db.query(Message).filter(Message.session_id == session.id).order_by(Message.created_at).all()
    assert [m.content for m in stored] == [content for _, content in STORED] + [CURRENT]


def test_agent_context_of_an_old_conversation(db):
    session = _conversation(db, "history-agent-old@example.com")
    db.add(Message(session_id=session.id, role="user", content=CURRENT, created_at=datetime.now(timezone.utc)))
    db.commit()

    messages, _ = agent_service._prepare_agent_context(db, session, CURRENT)

    _assert_old_conversation_context(messages)
    assert db.query(Message).filter(Message.session_id == session.id).count() == len(STORED) + 1


def test_chat_context_of_a_new_conversation(db):
    session = _conversation(db, "history-chat-new@example.com", stored=[])

    _, messages = chat_service._prepare_chat_context(db, session, CURRENT)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert "<capabilities>" in messages[0]["content"]
    assert messages[1]["content"] == CURRENT
