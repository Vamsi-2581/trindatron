"""Questions about what TRINDATRON can do, answered from the capability matrix.

"Você consegue criar vídeos?", "e imagens?", "can you search the internet?" are
questions about the product, and the product knows the answer: the capability
matrix says, for this person and this moment, whether each capability is
available, outside their plan, temporarily unavailable or not integrated yet.
Left to a model, the same question was answered from what language models in
general can do — "não consigo criar vídeos, procure no YouTube" while the
matrix said the plan was the reason — so these are answered here, before any
model runs, the same way identity probes are (prompting/identity_guard.py).

A question is not a request. "Você consegue criar uma imagem de um farol?"
names what to create, so it is a creation and goes to the media path; only a
question about the capability itself — nothing after the thing but "para
mim", "aqui", "ou vídeos" — is answered here. The wording of the answers is
the product's, per state; the state is always the matrix's.
"""
from __future__ import annotations

import re
import unicodedata

from src.prompting.identity_guard import detect_language


def _normalise(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.lower()).strip().lstrip("¿¡").strip()


# The thing asked about → the matrix capability that answers it. More specific first: "editar imagens" is
# image_editing, not image_generation; "ler imagens" is vision.
_TOPICS: tuple[tuple[str, str], ...] = (
    ("image_editing", r"(?:edit(?:ar|a|e|ing)?|retocar|alterar|modify|change|modificar) (?:\w+ )?(?:imagens?|fotos?|images?|photos?|imagenes|fotografias?)"),
    ("image_to_video", r"(?:animar|anima|animate|animating) (?:\w+ )?(?:imagens?|fotos?|images?|photos?|imagenes)"),
    ("vision", r"(?:ver|ler|analisar|entender|enxergar|reconhecer|descrever|see|read|analy[sz]e|understand|describe|look at|leer|analizar|describir) "
               r"(?:\w+ )?(?:imagens?|fotos?|images?|photos?|pictures?|imagenes|fotografias?)"),
    ("image_generation", r"(?:imagens?|fotos?|ilustrac(?:ao|oes)|desenhos?|figuras?|logos?|images?|pictures?|photos?|illustrations?|"
                         r"drawings?|imagenes|ilustraciones|dibujos)"),
    ("video_generation", r"(?:videos?|clipes?|clips?|animac(?:ao|oes)|animations?|animaciones)"),
    ("web_search", r"(?:internet|a web|na web|the web|la web|web|google|online|pesquis\w*|busca\w*|search\w*|"
                   r"noticias|news|informac(?:ao|oes) atua\w*|current information|informacion actual|tempo real|real ?time)"),
    ("browser_control", r"(?:navegador(?:es)?|browsers?|abrir sites?|open (?:web ?)?sites?|preencher formularios?|fill (?:in )?forms?)"),
    ("computer_control", r"(?:usar|controlar|operar|mexer n?o|use|control|operate) (?:o |um |a |el |un |my |meu |mi )?"
                         r"(?:computador|pc|computer|desktop|ordenador|computadora)"),
    ("code_execution", r"(?:executar|rodar|run|execute|ejecutar|correr) (?:\w+ )?(?:codigos?|code|scripts?|programas?|comandos?|commands?)"),
    ("document_generation", r"(?:apresentac(?:ao|oes)|slides?|presentations?|presentaciones|powerpoint|pptx|diapositivas)"),
    ("transcription", r"(?:transcrev\w*|transcri(?:cao|coes)|transcrib\w*|transcription|transcripci\w*)"),
    ("voice_conversation", r"(?:falar comigo|conversar por voz|voz|fala|audios?|speak|talk|voice|read aloud|ler em voz alta|hablar|habla)"),
    ("file_analysis", r"(?:pdfs?|documentos?|arquivos?|files?|documents?|archivos?|planilhas?|spreadsheets?|hojas de calculo)"),
    ("agent", r"(?:agentes?|agents?|modo agente|agent mode|modo de agente)"),
)
_TOPIC = r"\b(?:" + "|".join(f"(?:{pattern})" for _, pattern in _TOPICS) + r")\b"

_ABILITY = (
    r"(?:consegue|consegues|conseguiria|pode|podes|poderia|sabe|sabes|e capaz de|es capaz de|da (?:pra|para)|tem como|"
    r"faz|fazes|cria|crias|gera|geras|tem acesso|possui|suporta|oferece|trabalha com|lida com|acessa|"
    r"can|could|are you able to|do you|does trindatron|will you|is it possible to|"
    r"puedes|podrias|sabes|eres capaz de|haces|creas|generas|tienes acceso|es posible|ofreces)"
)
_SELF = r"(?:voce|vc|tu|trindatron|o trindatron|you|usted|el trindatron)"
_VERB = (
    r"(?:criar|gerar|fazer|produzir|mandar|enviar|me mandar|me enviar|editar|ver|ler|analisar|entender|acessar|usar|"
    r"pesquisar|buscar|navegar|executar|rodar|abrir|transcrever|falar|conversar|trabalhar com|lidar com|mexer com|"
    r"create|make|generate|produce|send|edit|see|read|analy[sz]e|understand|access|use|search|browse|run|open|"
    r"transcribe|speak|talk|work with|handle|"
    r"crear|hacer|generar|producir|enviar|editar|ver|leer|analizar|entender|acceder a|usar|buscar|navegar|ejecutar|"
    r"abrir|transcribir|hablar|trabajar con)"
)
_DET = r"(?:(?:a|o|as|os|um|uma|uns|umas|na|no|nas|nos|em|com|de|da|do|the|an?|some|any|on|in|with|la|el|las|los|un|una|unos|unas|en|con|del|al|mais|more|mas)\s+)"
# What may follow the thing asked about in a question about the capability itself, and nothing else.
_TAIL_WORD = (
    r"(?:para mim|pra mim|pra gente|para nos|aqui|por aqui|tambem|tbm|agora|hoje|no app|nesse app|neste app|ai|"
    r"for me|here|too|also|now|today|in the app|"
    r"para mi|aca|tambien|ahora|hoy|en la app)"
)
_ALTERNATIVE = rf"(?:(?:ou|e|nem|or|and|y|o|ni)\s+{_DET}?(?:{_TOPIC}))"
_TAIL = rf"(?:[\s,]+(?:{_TAIL_WORD}|{_ALTERNATIVE}))*"
_LEAD = r"^(?:(?:e|mas|ok|entao|certo|ta|hmm|nem|and|but|so|y|pero|entonces)[\s,.!]+)*"

_ABILITY_QUESTION = re.compile(
    rf"{_LEAD}(?:{_SELF}\s+)?{_ABILITY}\s+(?:{_SELF}\s+)?(?:{_VERB}\s+)?{_DET}?(?:{_TOPIC}){_TAIL}\s*[?!.]*$"
)
_ADDRESSED = re.compile(rf"{_LEAD}(?:{_SELF}\s|(?:can|could|do|does|will|are)\s+(?:you|trindatron)\b|is it possible\b|es posible\b)")
# "e vídeos?", "Nem. Vídeos?", "and the internet?" — a follow-up naming only the thing, as a question.
_ELLIPTIC_QUESTION = re.compile(rf"{_LEAD}(?:e quanto a |what about |how about |y )?{_DET}?(?:{_TOPIC}){_TAIL}\s*\?+$")


def asked_capabilities(text: str) -> tuple[str, ...]:
    """The matrix capabilities a question about TRINDATRON's own abilities asks about, in the order asked.

    Empty for anything else — including a polite request that names what to make
    ("você consegue criar uma imagem de um gato?"), which is a creation, not a question.
    """
    normalised = _normalise(text)
    if not normalised or len(normalised) > 160:
        return ()
    asked = "?" in normalised
    match = _ABILITY_QUESTION.match(normalised)
    # Without a question mark, "gera o vídeo" is an instruction; only "você gera vídeos" addresses TRINDATRON.
    if match is not None and not asked and not _ADDRESSED.match(normalised):
        return ()
    if match is None:
        if not asked or _ELLIPTIC_QUESTION.match(normalised) is None:
            return ()
    # Every mention counts, the tail's too ("imagens ou vídeos"). A generic mention inside a specific one
    # ("imagens" inside "editar imagens") belongs to the specific capability.
    spans: list[tuple[int, int, str]] = []
    for name, pattern in _TOPICS:
        for found in re.finditer(rf"\b(?:{pattern})\b", normalised):
            if not any(start <= found.start() and found.end() <= end for start, end, _ in spans):
                spans.append((found.start(), found.end(), name))
    ordered: list[str] = []
    for _, _, name in sorted(spans):
        if name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def is_capability_question(text: str) -> bool:
    return bool(asked_capabilities(text))


# ── Answers ──────────────────────────────────────────────────────────────────

_LABELS: dict[str, dict[str, str]] = {
    "image_generation": {"pt": "Criar imagens", "en": "Creating images", "es": "Crear imágenes"},
    "image_editing": {"pt": "Editar imagens", "en": "Editing images", "es": "Editar imágenes"},
    "image_to_video": {"pt": "Animar imagens em vídeo", "en": "Turning images into video", "es": "Animar imágenes en video"},
    "vision": {"pt": "Entender imagens", "en": "Understanding images", "es": "Entender imágenes"},
    "video_generation": {"pt": "Criar vídeos", "en": "Creating videos", "es": "Crear videos"},
    "web_search": {"pt": "Pesquisar na web", "en": "Searching the web", "es": "Buscar en la web"},
    "browser_control": {"pt": "Usar um navegador", "en": "Using a browser", "es": "Usar un navegador"},
    "computer_control": {"pt": "Usar um computador virtual", "en": "Using a virtual computer", "es": "Usar un computador virtual"},
    "code_execution": {"pt": "Executar código", "en": "Running code", "es": "Ejecutar código"},
    "document_generation": {"pt": "Criar apresentações", "en": "Creating presentations", "es": "Crear presentaciones"},
    "transcription": {"pt": "Transcrever áudio", "en": "Transcribing audio", "es": "Transcribir audio"},
    "voice_conversation": {"pt": "Conversar por voz", "en": "Talking by voice", "es": "Conversar por voz"},
    "file_analysis": {"pt": "Ler documentos", "en": "Reading documents", "es": "Leer documentos"},
    "agent": {"pt": "O modo agente", "en": "Agent mode", "es": "El modo agente"},
}

_HOW: dict[str, dict[str, str]] = {
    "image_generation": {"pt": "peça aqui na conversa, por exemplo “crie uma imagem de um farol ao entardecer”.",
                         "en": "ask here in the conversation, for example “create an image of a lighthouse at sunset”.",
                         "es": "pídelo aquí en la conversación, por ejemplo “crea una imagen de un faro al atardecer”."},
    "image_editing": {"pt": "anexe a imagem e diga o que mudar.", "en": "attach the image and say what to change.",
                      "es": "adjunta la imagen y di qué cambiar."},
    "image_to_video": {"pt": "anexe a imagem e peça para animá-la.", "en": "attach the image and ask to animate it.",
                       "es": "adjunta la imagen y pide animarla."},
    "vision": {"pt": "anexe a imagem e pergunte sobre ela.", "en": "attach the image and ask about it.",
               "es": "adjunta la imagen y pregunta sobre ella."},
    "video_generation": {"pt": "peça aqui na conversa, com a duração e o formato que quiser.",
                         "en": "ask here in the conversation, with the length and format you want.",
                         "es": "pídelo aquí en la conversación, con la duración y el formato que quieras."},
    "web_search": {"pt": "pergunte sobre algo atual — notícias, preços, lançamentos — e a web é pesquisada.",
                   "en": "ask about something current — news, prices, releases — and the web is searched.",
                   "es": "pregunta por algo actual — noticias, precios, lanzamientos — y se busca en la web."},
    "browser_control": {"pt": "peça no modo agente para abrir, ler ou preencher uma página.",
                        "en": "ask agent mode to open, read or fill in a page.",
                        "es": "pide al modo agente abrir, leer o completar una página."},
    "computer_control": {"pt": "peça no modo agente; ele usa um computador separado, nunca o seu aparelho.",
                         "en": "ask in agent mode; it uses a separate computer, never your own device.",
                         "es": "pídelo en modo agente; usa un computador aparte, nunca tu dispositivo."},
    "code_execution": {"pt": "peça no modo agente; o código roda em um ambiente isolado.",
                       "en": "ask in agent mode; the code runs in an isolated environment.",
                       "es": "pídelo en modo agente; el código se ejecuta en un entorno aislado."},
    "document_generation": {"pt": "peça uma apresentação sobre o assunto que quiser.",
                            "en": "ask for a presentation on any subject.",
                            "es": "pide una presentación sobre el tema que quieras."},
    "transcription": {"pt": "use o microfone ou envie o áudio.", "en": "use the microphone or send the recording.",
                      "es": "usa el micrófono o envía el audio."},
    "voice_conversation": {"pt": "toque no microfone para falar; a resposta pode ser ouvida.",
                           "en": "tap the microphone to speak; the answer can be heard.",
                           "es": "toca el micrófono para hablar; la respuesta se puede escuchar."},
    "file_analysis": {"pt": "anexe o documento e pergunte sobre ele.", "en": "attach the document and ask about it.",
                      "es": "adjunta el documento y pregunta sobre él."},
    "agent": {"pt": "peça uma tarefa de várias etapas começando com “Aja como agente”.",
              "en": "ask for a multi-step task starting with “Act as an agent”.",
              "es": "pide una tarea de varios pasos empezando con “Actúa como agente”."},
}

_STATE_SENTENCE: dict[str, dict[str, str]] = {
    "available": {"pt": "{label} está disponível para você agora: {how}",
                  "en": "{label} is available to you now: {how}",
                  "es": "{label} está disponible para ti ahora: {how}"},
    "plan": {"pt": "{label} não está incluído no seu plano atual; está disponível em planos que incluem esse recurso.",
             "en": "{label} is not included in your current plan; it is available on plans that include it.",
             "es": "{label} no está incluido en tu plan actual; está disponible en los planes que lo incluyen."},
    "unavailable": {"pt": "{label} faz parte do TRINDATRON, mas está temporariamente indisponível agora.",
                    "en": "{label} is part of TRINDATRON, but it is temporarily unavailable right now.",
                    "es": "{label} es parte de TRINDATRON, pero no está disponible temporalmente en este momento."},
    "not_integrated": {"pt": "{label} ainda não está integrado ao TRINDATRON.",
                       "en": "{label} is not integrated into TRINDATRON yet.",
                       "es": "{label} todavía no está integrado en TRINDATRON."},
}


def capability_reply(
    text: str, matrix: dict[str, dict], names: tuple[str, ...] | None = None, language: str | None = None
) -> str:
    """The product's answer to a capability question, one sentence per capability asked, from `matrix` alone.

    A capability the matrix does not list is not integrated: the product never claims what it cannot show.
    `language` is the resolved answer language (src/language.py); without one the text decides, as before.
    """
    language = language if language in _STATE_SENTENCE["available"] else detect_language(text)
    sentences = []
    for name in names if names is not None else asked_capabilities(text):
        state = matrix.get(name) or {}
        status = state.get("status") or ("available" if state.get("available") else "not_integrated")
        if status not in _STATE_SENTENCE:
            status = "unavailable"
        sentence = _STATE_SENTENCE[status][language].format(
            label=_LABELS[name][language], how=_HOW[name][language]
        )
        sentences.append(sentence)
    return " ".join(sentences)


_UNCHECKED = {
    "pt": "Por isso, informações de agora não podem ser verificadas nesta conversa.",
    "en": "So current information cannot be checked in this conversation.",
    "es": "Por eso, la información actual no se puede verificar en esta conversación.",
}


def search_unavailable_reply(text: str, matrix: dict[str, dict], language: str | None = None) -> str | None:
    """For a request that needs the web right now: the reason it cannot be done, from the matrix — or None when web
    search is available and the request should go on to the tool loop."""
    state = matrix.get("web_search") or {}
    if state.get("status") == "available" or (state.get("status") is None and state.get("available")):
        return None
    language = language if language in _UNCHECKED else detect_language(text)
    return f"{capability_reply(text, matrix, ('web_search',), language)} {_UNCHECKED[language]}"
