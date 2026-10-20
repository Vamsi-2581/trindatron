"""PresentationProvider — the deck-engine boundary.

TRINDATRON owns the capability; whatever renders the deck is infrastructure
behind this interface. Nothing above it imports Presenton, knows its URL,
its auth shape, or its request body — so replacing the engine is a new class
here and nothing else.

Shaped from the REAL self-hosted API, exercised against a live container on
2026-09-11, not from the published reference. Two things differ from the
documentation and both matter:

  * the docs say Basic Auth; the running API uses a session cookie obtained
    from POST /api/v1/auth/login
  * generation is synchronous and can run for minutes, so the caller must
    treat it as a job, never as part of a request/response cycle

The interface is deliberately narrower than Presenton's surface. It exposes
what a presentation capability needs — can you run, make me a deck, give me
the bytes — and nothing about slides, templates or editing, so a future
engine with a different internal model can still satisfy it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ExportFormat(str, Enum):
    PPTX = "pptx"
    PDF = "pdf"


class ImageStrategy(str, Enum):
    """How a deck gets its pictures.

    The economics live here. A generated image costs $0.045–$0.151 each
    (Gemini image pricing, 2026-09); a stock photograph costs nothing beyond
    the API call. On a 15-slide deck that is the difference between ~17 TCU
    and 85–244 TCU — which is the difference between a deck fitting inside a
    START window and one costing eight times the whole window.

    STOCK_FIRST is the default for exactly that reason: use a real photograph
    when one will do, and spend on generation only where a specific image has
    to be invented.
    """
    STOCK_FIRST = "stock_first"
    STOCK_ONLY = "stock_only"
    GENERATED = "generated"
    NONE = "none"


@dataclass(frozen=True)
class PresentationRequest:
    """What the user asked for, in engine-neutral terms."""
    content: str
    n_slides: int = 10
    language: str = "English"
    export_as: ExportFormat = ExportFormat.PPTX
    image_strategy: ImageStrategy = ImageStrategy.STOCK_FIRST
    web_search: bool = False
    instructions: str | None = None
    include_title_slide: bool = True
    # Workspace-relative paths already uploaded to the engine, if any.
    source_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PresentationArtifact:
    """One produced file, as bytes plus what is needed to store it."""
    filename: str
    media_type: str
    data: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class PresentationResult:
    provider: str
    external_id: str | None
    artifacts: list[PresentationArtifact]
    duration_s: float
    # Provider-reported model calls, when the provider can tell us. None
    # means "not reported" — never zero, which would read as a free deck.
    model_calls: int | None = None


@dataclass(frozen=True)
class ProviderAvailability:
    """Whether this provider can run right now, and why not if it cannot.

    `reason` is for operators and logs. It is never shown to a user: a
    visitor asking for a deck gets an offer of something else, not a
    diagnostic about a container.
    """
    available: bool
    reason: str = ""


class PresentationError(RuntimeError):
    """Generation failed for a reason the caller should surface as a failure
    rather than retry blindly."""


class PresentationQuotaError(PresentationError):
    """The underlying model provider refused on quota or rate limits.

    Separated from PresentationError because the correct response differs:
    a quota refusal must NOT be retried harder — that spends the remaining
    allowance faster and makes the outage longer. The job fails and says so.
    """


class PresentationProvider(ABC):
    """A deck engine."""

    #: Short stable name for logs, metrics and the job's provider_name column.
    name: str = "abstract"

    @abstractmethod
    async def availability(self) -> ProviderAvailability:
        """Can this provider run a job right now? Must never raise."""

    @abstractmethod
    async def generate(self, request: PresentationRequest) -> PresentationResult:
        """Produce a deck. May take minutes — callers run this inside a job.

        Raises PresentationQuotaError when the model provider refused on
        quota, PresentationError for anything else.
        """
