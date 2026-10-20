"""PresentationLlmCall — one model call the presentation engine made.

Why this table exists
---------------------
Presenton is a separate service. It calls a model once for the outline and
once per slide, and it reports none of that back: the generate response
carries a presentation id and a file path, no token counts. Without a record
of its own, TRINDATRON would have to finalize a deck's usage against the
pre-flight ESTIMATE, permanently, and an estimate that is never reconciled is
just a guess that bills.

So the engine is pointed at TRINDATRON's own gateway rather than at Google
(see api/routes/presenton_gateway.py), and the gateway writes one row here per
call with the real token counts Google returned. The job then sums the rows
its own generation produced and finalizes against measured cost.

Attribution without a correlation id
------------------------------------
The engine has no way to carry a job id: CUSTOM_LLM_API_KEY is fixed
environment read once at container start, and the OpenAI-compatible request
body has no field we could smuggle one through. Attribution is therefore
temporal — the job sums rows created between the instant it started
generating and the instant the engine returned.

That is exact rather than approximate because generation is single-flight by
construction: PresentonProvider.generate() holds an asyncio lock for the whole
call, and the engine is deployed as a single scaled-to-zero instance. Two
decks cannot be generating at once, so no other job's calls can fall inside
the window. If the engine is ever scaled to several replicas, THIS is the
assumption that breaks and a real correlation id becomes necessary — which is
why it is written down here rather than left to be rediscovered.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PresentationLlmCall(Base):
    __tablename__ = "presentation_llm_calls"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # The model actually used, as the gateway resolved it — never what the
    # engine asked for. Recorded so a drift between configured and used model
    # is visible in the data rather than only in a log line.
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Reasoning tokens are billed at the output rate and are NOT included in
    # output_tokens by the OpenAI-compatible response shape, so they are kept
    # separately rather than folded in — a deck's reasoning is roughly 2.5x
    # its visible completion, which is far too large to lose.
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
