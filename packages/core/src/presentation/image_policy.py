"""Which pictures a deck gets, and why — decided without spending anything.

The economics, measured (see cost.py)
-------------------------------------
A generated image costs up to $0.151. Stock photography costs nothing beyond
an API call. On a 10-slide deck that is the difference between 12.3 TCU and
163.3 TCU — a 13x multiplier on the whole job, against a START window of 30
TCU and a PRO window of 60. Generating an image for every slide by default
would mean a single deck consumes several windows' worth of budget, so the
default is stock and generation has to earn its place.

Two independent gates, and the stricter one wins
------------------------------------------------
  plan ceiling   what the plan's window can absorb at all. START and PRO
                 cannot pay for a generated deck under any circumstances,
                 so for them this is the end of the decision.
  brief need     whether stock photography can actually represent this
                 subject. A deck about "our Q3 sales results in São Paulo"
                 is well served by stock; a deck about "the TRINDATRON
                 execution pipeline" is not, because no stock library
                 contains it.

A rule, not a model
-------------------
The subject test is a keyword/regex classifier over the brief, deliberately
not an LLM call. Adding one would mean spending a model call to decide
whether to spend a model call, on a judgement that is mostly lexical. It is
also transparent: every decision returns the reason that produced it, so a
surprising outcome can be read off rather than guessed at.

What this cannot do, honestly
-----------------------------
Presenton selects its image source from process-wide environment
(IMAGE_PROVIDER / DISABLE_IMAGE_GENERATION — see presenton.image_provider_env),
so the choice is made once per deck, not once per slide. A genuine
"stock where stock works, generate only the three slides where it doesn't"
would need per-slide control the engine's REST API does not expose. Rather
than pretend otherwise, the policy picks the best whole-deck answer and
says so; if the engine later gains per-slide control, this module is where
that lands and cost.py's image term is what changes with it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.presentation.provider import ImageStrategy

# Plans whose window can absorb generated imagery at all. Derived from the
# measured figures above, not chosen for tiering: a 10-slide generated deck
# is ~163 TCU, which exceeds START's entire 30-TCU session window and PRO's
# 60 several times over. MAX (200) and TEAM can carry one.
GENERATED_CAPABLE_PLANS = frozenset({"max", "team"})

# Subjects a stock library genuinely contains. Broad, concrete, photographable.
_STOCK_SUFFICIENT = re.compile(
    r"\b("
    r"business|meeting|team|office|startup|market|sales|finance|budget|revenue"
    r"|travel|tourism|city|cities|country|nature|landscape|climate|environment"
    r"|food|restaurant|health|fitness|medicine|hospital|school|education|student"
    r"|sport|music|culture|history|people|customer|interview|conference"
    r"|results|report|quarter|q[1-4]|board|strategy|roadmap|growth|product"
    # pt-BR / es
    r"|neg[oó]cios|reuni[aã]o|equipe|escrit[oó]rio|mercado|vendas|finan[cç]as"
    r"|or[cç]amento|receita|viagem|turismo|cidade|natureza|paisagem|meio.ambiente"
    r"|comida|sa[uú]de|educa[cç][aã]o|esporte|m[uú]sica|cultura|hist[oó]ria|pessoas"
    r"|empresa|clientes|ventas|reuni[oó]n|equipo|oficina|ciudad|naturaleza|salud"
    r"|educaci[oó]n|deporte|m[uú]sica|historia|personas"
    r")\b",
    re.IGNORECASE,
)

# Subjects no stock library can represent: things that are specific to one
# product, invented, diagrammatic, or otherwise not photographable.
_NEEDS_GENERATION = re.compile(
    r"\b("
    r"architecture|pipeline|workflow|dashboard|interface|wireframe|mockup"
    r"|prototype|concept.art|illustration|diagram|schematic|blueprint"
    r"|futuristic|sci.?fi|fantasy|imaginary|fictional|surreal"
    r"|abstract\s+(?:art|background|shape|pattern|illustration|render)"
    r"|mascot|logo.concept|character.design|storyboard"
    # pt-BR / es
    r"|arquitetura|fluxo|painel|interface|prot[oó]tipo|ilustra[cç][aã]o|diagrama"
    r"|esquema|futurista|fic[cç][aã]o|imagin[aá]rio|ficticio|surreal"
    r"|mascote|arquitectura|flujo|ilustraci[oó]n|diagrama|prototipo"
    r"|fundo\s+abstrato|arte\s+abstrata|arte\s+abstracto"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImageDecision:
    """What was chosen, and the reason — carried so a surprising choice can
    be explained without re-running the classifier."""

    strategy: ImageStrategy
    reason: str
    # True when the brief wanted generation but the plan could not pay for
    # it. The caller surfaces this as a capability note, never as an error:
    # a stock deck is still a real deck.
    downgraded_for_plan: bool = False


def plan_ceiling(plan: str) -> ImageStrategy:
    """The most expensive imagery this plan's window can carry."""
    return (
        ImageStrategy.GENERATED
        if plan.lower() in GENERATED_CAPABLE_PLANS
        else ImageStrategy.STOCK_FIRST
    )


def brief_need(content: str) -> tuple[ImageStrategy, str]:
    """What the subject itself calls for, ignoring who is paying."""
    text = content or ""
    needs_generation = bool(_NEEDS_GENERATION.search(text))
    stock_sufficient = bool(_STOCK_SUFFICIENT.search(text))

    if needs_generation and not stock_sufficient:
        return ImageStrategy.GENERATED, "subject is not photographable from stock"
    if needs_generation and stock_sufficient:
        # Mixed brief. Stock covers the concrete half, and the abstract half
        # is usually one or two slides — not worth a 13x multiplier on the
        # whole deck, which is the only granularity the engine offers.
        return ImageStrategy.STOCK_FIRST, "mixed subject; stock covers the photographable majority"
    if stock_sufficient:
        return ImageStrategy.STOCK_FIRST, "subject is well covered by stock photography"
    return ImageStrategy.STOCK_FIRST, "no signal that stock would fail; defaulting to the cheaper source"


def decide(*, plan: str, content: str, requested: ImageStrategy | None = None) -> ImageDecision:
    """The whole rule: the stricter of the plan ceiling and the brief's need.

    `requested` lets an explicit user choice narrow the outcome, never widen
    it — asking for NONE is always honoured (it is cheaper), asking for
    GENERATED is still subject to the plan ceiling.
    """
    ceiling = plan_ceiling(plan)

    if requested is ImageStrategy.NONE:
        return ImageDecision(ImageStrategy.NONE, "images turned off for this deck")

    wanted, reason = brief_need(content)
    if requested is ImageStrategy.GENERATED:
        wanted, reason = ImageStrategy.GENERATED, "generation requested for this deck"
    elif requested in (ImageStrategy.STOCK_FIRST, ImageStrategy.STOCK_ONLY):
        return ImageDecision(requested, "stock requested for this deck")

    if wanted is ImageStrategy.GENERATED and ceiling is not ImageStrategy.GENERATED:
        return ImageDecision(
            ImageStrategy.STOCK_FIRST,
            "generated imagery costs more than this plan's window can carry; using stock",
            downgraded_for_plan=True,
        )
    return ImageDecision(wanted, reason)
