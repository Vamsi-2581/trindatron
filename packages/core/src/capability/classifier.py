"""
Capability Classifier — Layer 3 of the TRINITY architecture.

Classifies raw user input into one of 10 capabilities using deterministic
regex rules. No LLM call is made here. The classifier is intentionally
conservative: unmatched input falls back to CHAT.

Rule ordering matters: more specific patterns appear first so they take
precedence over broader ones (e.g., IMAGE before RESEARCH).

Language support: English, Portuguese (pt-BR), Spanish (es).
Adding a new language requires extending the relevant alternation groups below.
"""
from __future__ import annotations

import re
from enum import Enum


class Capability(str, Enum):
    CHAT = "chat"
    RESEARCH = "research"
    CODE = "code"
    WORK = "work"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILES = "files"
    AUTOMATION = "automation"
    AGENT = "agent"
    PRESENTATION = "presentation"


# Each tuple is (compiled pattern, capability). Rules are evaluated in order;
# the first match wins. CHAT is the implicit fallback (no rule needed).
_RULES: list[tuple[re.Pattern[str], Capability]] = [
    # PRESENTATION — deck generation (EN / PT-BR / ES).
    #
    # Placed BEFORE the IMAGE rule deliberately. "create a presentation about
    # solar energy" contains no image verb, but "make me some slides and
    # illustrate them" contains both — and a request naming a deck is a deck
    # request whatever else it mentions. First match wins, so order is the
    # whole decision here.
    (re.compile(
        r"\b("
        # EN
        r"(?:creat|mak|build|generat|draft|prepar|put\s+together)\w*\s+"
        r"(?:an?\s+|me\s+an?\s+|the\s+)?"
        r"(?:\d+[\s-]*slide[\s-]*)?(?:presentation|slide\s*deck|deck|slides)|"
        r"\bpowerpoint\b|\bpptx\b|\bkeynote\s+deck\b|"
        # PT-BR
        r"(?:cri|faç|monta?r?|prepar|gera?r?|elabor)\w*\s+"
        r"(?:uma?\s+|o\s+)?(?:\d+[\s-]*slides?[\s-]*)?"
        r"(?:apresenta[çc][ãa]o|slides?|deck)|"
        # ES
        r"(?:cre|ha[zg]|prepar|gener|elabor|arm)\w*\s+"
        r"(?:una?\s+|el\s+)?(?:\d+[\s-]*diapositivas?[\s-]*)?"
        r"(?:presentaci[óo]n|diapositivas?|deck)"
        r")",
        re.IGNORECASE | re.UNICODE,
    ), Capability.PRESENTATION),

    # IMAGE — visual generation keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"generate\s+(?:an?\s+)?image|create\s+(?:an?\s+)?image|draw|paint|illustrate|"
        r"render\s+(?:a|an)\s+\w+|dalle|midjourney|stable\s+diffusion|"
        r"(?:make|generate|create)\s+(?:me\s+)?(?:an?\s+)?(?:picture|photo|illustration|logo)|"
        r"make\s+(?:me\s+)?an?\s+image|"
        # A creation verb, then any article or pronoun the person puts before the thing ("uma", "a", "essa",
        # "outra"). "Crie a imagem dele" is as much a request as "crie uma imagem"; without the article it
        # reached a model, which answered in words instead of creating anything.
        r"(?:make|generate|create|design|produce)\s+(?:me\s+)?(?:(?:an?|the|this|that|another|its|his|her|their)\s+)?"
        r"(?:image|picture|photo|illustration|logo)s?|"
        # PT-BR
        r"(?:ger(?:e|a|ar)|gener(?:e|a|ar)|cri(?:e|a|ar)|fa[çc]a|faz(?:er)?|monte|montar|produza|produzir|me\s+(?:d[aáê]|mande|manda))\s+"
        r"(?:(?:uma?|a|as|umas|essa|esta|aquela|outra|mais\s+uma|nova|minha|sua)\s+)?"
        r"(?:imag(?:em|ens)|fotos?|ilustra[çc](?:[ãa]o|[õo]es)|figuras?|arte|logo(?:tipo)?)|"
        r"quero\s+uma\s+imagem|"
        r"desen(?:he|har)|pint(?:e|ar)|ilustr(?:e|ar)|"
        # ES
        r"(?:gen(?:era|erar|ere)|cre(?:a|ar|e)|haz(?:me)?|hacer)\s+(?:(?:una?|la|las|esa|esta|otra)\s+)?"
        r"(?:im[aá]gen(?:es)?|fotos?|ilustraci[óo]n(?:es)?|logos?)|"
        r"dibuj(?:a|ar)|pint(?:a|ar)\s+(?:una?\s+)?\w+|ilustr(?:a|ar)"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.IMAGE),

    # VIDEO — motion/animation keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"generate\s+(?:a\s+)?video|create\s+(?:a\s+)?video|animate|animation|"
        r"(?:make|want)\s+(?:me\s+)?(?:a\s+)?video|"
        r"movie\s+clip|mp4|sora|"
        # PT-BR — the verbs people actually use for a video, the same set IMAGE already
        # accepts for an image. Without them "Faça um vídeo a partir dessa foto" fell
        # through to CHAT and a model answered it in words instead of creating anything.
        # Any article or pronoun before the video, as for images: "gera o vídeo", "crie esse vídeo".
        r"(?:ger(?:e|a|ar)|gener(?:e|a|ar)|cri(?:e|a|ar)|fa[çc]a|fazer|faz|fa[çc]|monte|montar|quero|queria|produza|produzir|"
        r"me\s+(?:d[aáê]|mande|manda))\s+(?:(?:um|o|os|uns|esse|este|aquele|outro|mais\s+um|novo|meu|seu)\s+)?v[ií]deos?|"
        r"anim(?:e|ar)|anima[cç][aã]o|clipe\s+de\s+v[ií]deo|"
        # ES
        r"(?:gen(?:era|erar|ere)|cre(?:a|ar|e)|ha[zg](?:me)?|hacer|quiero|arm(?:a|ar))\s+"
        r"(?:(?:un|el|los|ese|este|otro)\s+)?videos?|"
        r"anim(?:a|ar)\s+\w+|animaci[oó]n"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.VIDEO),

    # AUDIO — sound/speech keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"transcribe|text.to.speech|tts|speech.to.text|"
        r"generate\s+(?:an?\s+)?audio|podcast|narrat(?:e|ion)|mp3|wav\s+file|"
        # PT-BR
        r"transcrev(?:a|er)|transcrever|texto\s+para\s+(?:fala|voz)|"
        r"gen(?:ere|era)\s+(?:um\s+)?[aá]udio|narr(?:e|ar)|"
        # ES
        r"transcrib(?:e|ir)|texto\s+a\s+voz|"
        r"gen(?:era|erar)\s+(?:un\s+)?audio|narr(?:a|ar)\s+\w+"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.AUDIO),

    # CODE — programming and software keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"write\s+(?:a\s+)?(?:function|class|script|program|test)|"
        r"debug|refactor|implement\s+\w|fix\s+(?:the\s+)?(?:bug|error|code)|"
        r"compile|unit\s+test|algorithm|"
        # PT-BR
        r"escrev(?:a|er)\s+(?:uma?\s+)?(?:fun[cç][aã]o|classe|script|programa|teste)|"
        r"depurar|depure|refatorar|refatore|"
        r"implement(?:e|ar)\s+\w|corri(?:ja|gir)\s+(?:o\s+)?(?:bug|erro|c[oó]digo)|"
        r"compilar|teste\s+unit[aá]rio|algoritmo|"
        # ES
        r"escrib(?:e|ir)\s+(?:una?\s+)?(?:funci[oó]n|clase|script|programa|prueba)|"
        r"depurar|refactorizar|refactori[zs](?:a|ar)|"
        r"implement(?:a|ar)\s+\w|corri(?:ge|gir)\s+(?:el\s+)?(?:bug|error|c[oó]digo)|"
        r"compilar|prueba\s+unitaria|algoritmo"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.CODE),

    # WORK — browser/web-automation keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"browse\s+(?:to|the)|click\s+(?:on\s+)?(?:the\s+)?\w+|"
        r"fill\s+(?:in\s+)?(?:the\s+)?form|automate\s+(?:the\s+)?website|"
        r"navigate\s+to\s+(?:https?://|\w+)|"
        # PT-BR
        r"naveg(?:ue|ar)\s+para|clique?\s+(?:em|no|na)\s+\w+|"
        r"preencha?\s+(?:o\s+)?formul[aá]rio|automatize?\s+(?:o\s+)?site|"
        r"acesse?\s+(?:o\s+)?site|"
        # ES
        r"naveg(?:a|ar)\s+(?:a|hacia)|haz\s+clic\s+en\s+\w+|"
        r"rellen(?:a|ar)\s+(?:el\s+)?formulario|automatiz(?:a|ar)\s+(?:el\s+)?sitio|"
        # Open / visit a link — "Abra https://example.com" was classified as chat, reached no tool, and the model
        # answered that web access was unavailable on an account that has it. A bare domain counts only with a
        # website TLD, so "abra o relatorio.pdf" stays a file request.
        r"(?:open|visit|go\s+to|abr(?:a|ir|e)|visite?|visitar|entr(?:e|ar|a)\s+(?:no|na|em|en)|acced(?:e|er)\s+a)\s+"
        r"(?:(?:o|a|the|el|la)\s+)?(?:(?:site|website|p[aá]gina|page|link|sitio|enlace)\s+)?"
        r"(?:https?://|www\.|[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|app|ai|gov|edu|info|co|me|tv|br|pt|es|us|uk)\b)"
        r")",
        re.IGNORECASE | re.UNICODE,
    ), Capability.WORK),

    # FILES — explicit file-operation keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"read\s+(?:the\s+)?file|write\s+(?:to\s+)?(?:the\s+)?file|"
        r"upload\s+(?:a\s+)?file|download\s+(?:the\s+)?file|"
        r"parse\s+(?:the\s+)?(?:csv|json|pdf|xml)|open\s+(?:the\s+)?file|"
        # PT-BR
        r"l(?:eia|er)\s+(?:o\s+)?arquivo|escrev(?:a|er)\s+(?:no\s+)?arquivo|"
        r"fa(?:ça|zer)\s+upload\s+(?:do\s+)?arquivo|"
        r"baixar?\s+(?:o\s+)?arquivo|analise?\s+(?:o\s+)?(?:csv|json|pdf|xml)|"
        r"abrir?\s+(?:o\s+)?arquivo|"
        # ES
        r"le(?:e|er)\s+(?:el\s+)?archivo|escrib(?:e|ir)\s+(?:en\s+)?(?:el\s+)?archivo|"
        r"subir?\s+(?:el\s+)?archivo|descargar?\s+(?:el\s+)?archivo|"
        r"analiz(?:a|ar)\s+(?:el\s+)?(?:csv|json|pdf|xml)|abrir?\s+(?:el\s+)?archivo"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.FILES),

    # AUTOMATION — workflow/pipeline keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"automate\s+(?:this|the|my)\s+\w+|schedule\s+(?:a\s+)?(?:job|task|cron)|"
        r"create\s+(?:a\s+)?(?:workflow|pipeline)|set\s+up\s+(?:a\s+)?webhook|"
        # PT-BR
        r"automatize?\s+(?:este|essa?|minha?\s+)\w+|"
        r"agend(?:e|ar)\s+(?:uma?\s+)?(?:tarefa|job|cron)|"
        r"crie?\s+(?:um\s+)?(?:workflow|pipeline|fluxo)|"
        r"configur(?:e|ar)\s+(?:um\s+)?webhook|"
        # ES
        r"automatiz(?:a|ar)\s+(?:esto|esta?\s+)\w+|"
        r"program(?:a|ar)\s+(?:una?\s+)?(?:tarea|job|cron)|"
        r"cre(?:a|ar)\s+(?:un\s+)?(?:workflow|pipeline|flujo)|"
        r"configur(?:a|ar)\s+(?:un\s+)?webhook"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.AUTOMATION),

    # AGENT — explicit agentic keywords (EN / PT-BR / ES)
    (re.compile(
        r"\b("
        # EN
        r"act\s+as\s+(?:an?\s+)?agent|multi.?step\s+task|plan\s+and\s+execute|"
        r"run\s+(?:an?\s+)?agent|"
        # PT-BR
        r"aja?\s+como\s+(?:um\s+)?agente|tarefa\s+(?:de\s+)?m[uú]ltiplos?\s+passos?|"
        r"planej(?:e|ar)\s+e\s+execut(?:e|ar)|execut(?:e|ar)\s+como\s+agente|"
        # ES
        r"act[uú](?:a|ar)\s+como\s+(?:un\s+)?agente|tarea\s+de\s+m[uú]ltiples?\s+pasos?|"
        r"planific(?:a|ar)\s+y\s+ejecut(?:a|ar)"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.AGENT),

    # RESEARCH — search/analysis keywords (EN / PT-BR / ES) — broad, comes last
    (re.compile(
        r"\b("
        # EN
        r"search\s+(?:for|the\s+web|online|the\s+internet)|look\s+up|"
        r"find\s+(?:information|data|details)\s+(?:about|on)|"
        r"research\s+(?:the\s+topic|this)|summarize\s+(?:the\s+latest|recent)|"
        # PT-BR
        r"pesquise?|pesquisar|busque?|buscar\s+(?:informa[cç][oõ]es|dados)|"
        r"encontre?\s+informa[cç][oõ]es\s+(?:sobre|em)|"
        r"fa[cç]a\s+uma\s+pesquisa|resuma\s+(?:os\s+)?(?:[uú]ltimos?|recentes?)|"
        # ES
        r"busca(?:r)?\s+(?:en\s+la\s+web|informaci[oó]n|datos)|investiga(?:r)?|"
        r"encuentra\s+informaci[oó]n\s+(?:sobre|en)|"
        r"resum(?:e|ir)\s+(?:los\s+)?(?:[uú]ltimos?|recientes?)"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.RESEARCH),

    # ── A creation described, not ordered ── (last: every explicit rule above wins)
    # "Vídeo de um barco no pôr do sol" or a scene written the way a video is shot ("Wide cinematic shot at
    # golden hour… camera dolly…") is a request for that video, even with no "crie". Reaching a model instead,
    # it was answered "video creation is not available" (production, 2026-09-27) — while it was.
    (re.compile(
        r"^\s*(?:(?:um|o|an?|the|un|el)\s+)?v[íi]deo\s+(?:de|do|da|dos|das|com|sobre|of|about|showing|with|con|del)\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.VIDEO),
    (re.compile(
        r"^\s*(?:(?:uma|a|an?|the|una|la)\s+)?(?:imagem|foto(?:grafia)?|ilustra[çc][ãa]o|image|photo(?:graph)?|picture|"
        r"illustration|imagen|ilustraci[óo]n)\s+(?:de|do|da|dos|das|com|of|showing|with|con|del)\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.IMAGE),
    (re.compile(
        r"\b(?:"
        # EN — shot types and camera movement
        r"(?:wide|close[\s-]?up|medium|aerial|drone|tracking|establishing|cinematic|dolly|panning|handheld|overhead|"
        r"low[\s-]angle|high[\s-]angle)\s+(?:shot|footage|camera|angle)|"
        r"camera\s+(?:dolly|pans?|tilts?|moves?|tracks?|follows?|orbits?|zooms?|push(?:es)?\s+in)|"
        r"(?:gentle|slow|smooth)\s+camera|slow[\s-]?motion|time[\s-]?lapse|b[\s-]?roll|with\s+(?:audio|sound)\s*:|"
        # PT-BR
        r"plano\s+(?:geral|aberto|fechado|m[ée]dio|a[ée]reo|cinematogr[áa]fico)|tomada\s+a[ée]rea|"
        r"c[âa]mera\s+(?:lenta|se\s+move|acompanha|gira|segue|se\s+aproxima|se\s+afasta)|imagens?\s+de\s+drone|"
        r"(?:v[íi]deo|cena)\s+cinematogr[áa]fic[oa]|"
        # ES
        r"plano\s+(?:general|a[ée]reo|cinematogr[áa]fico)|c[áa]mara\s+(?:lenta|se\s+mueve|sigue)"
        r")\b",
        re.IGNORECASE | re.UNICODE,
    ), Capability.VIDEO),
]


def classify(user_content: str) -> Capability:
    """Classify user input into a Capability. Returns CHAT if no rule matches.

    A question about what TRINDATRON can do ("você consegue criar imagens?") is conversation, not a
    creation: it is answered from the capability matrix (capability/questions.py) and never starts a job.
    """
    from src.capability.questions import is_capability_question

    if is_capability_question(user_content):
        return Capability.CHAT
    for index, (pattern, capability) in enumerate(_RULES):
        if index >= _DESCRIBED_FROM and _ASKS_ABOUT.search(user_content):
            break  # "o que é time-lapse?" is a question about a technique, not a scene to make
        if pattern.search(user_content):
            return capability
    return Capability.CHAT


# Where the "creation described, not ordered" rules start: the last three entries of _RULES.
_DESCRIBED_FROM = len(_RULES) - 3
_ASKS_ABOUT = re.compile(
    r"\?\s*$|^\s*(?:o\s+que|qual|quais|como|por\s*qu[eê]|quando|onde|me\s+explique|explique|explica|what|which|how|why|"
    r"when|where|explain|tell\s+me|qu[ée]|cu[áa]l|c[óo]mo|por\s+qu[ée]|expl[ií]ca(?:me)?)\b",
    re.IGNORECASE | re.UNICODE,
)
