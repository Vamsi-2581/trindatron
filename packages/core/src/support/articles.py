"""Help center articles — short, true, and in every product language.

Each article describes what TRINDATRON actually does today. Nothing here names
an engine, model or vendor; the product is TRINDATRON.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

LANGUAGES = ("pt", "en", "es")


@dataclass(frozen=True)
class Article:
    id: str
    category: str
    title: dict[str, str]
    body: dict[str, str]
    keywords: tuple[str, ...]


ARTICLES: tuple[Article, ...] = (
    Article(
        "stay-signed-in", "account",
        {"pt": "Por que preciso entrar de novo?", "en": "Why do I have to sign in again?", "es": "¿Por qué tengo que volver a iniciar sesión?"},
        {
            "pt": "Depois de entrar, você continua conectado até tocar em Sair — inclusive ao bloquear a tela, fechar ou reiniciar o app. Se aparecer \"Sua sessão terminou\", a sessão foi encerrada por segurança (por exemplo, Sair em outro aparelho ou uso indevido detectado). Entre novamente para continuar.",
            "en": "Once signed in, you stay signed in until you tap Sign out — including after locking the screen, closing or restarting the app. If you see \"Your session has ended\", it was ended for security (for example, signing out elsewhere or detected misuse). Sign in again to continue.",
            "es": "Después de iniciar sesión, sigues conectado hasta tocar Salir, incluso al bloquear la pantalla, cerrar o reiniciar la app. Si ves \"Tu sesión terminó\", se cerró por seguridad (por ejemplo, al salir en otro dispositivo o por uso indebido detectado). Inicia sesión de nuevo para continuar.",
        },
        ("entrar", "login", "sessão", "sessao", "sign in", "session", "logout", "sair", "desconectado", "iniciar sesión", "sesion", "senha", "password"),
    ),
    Article(
        "google-sign-in", "account",
        {"pt": "Entrar com Google", "en": "Sign in with Google", "es": "Iniciar sesión con Google"},
        {
            "pt": "Use \"Continuar com Google\" em Entrar ou Criar conta. Se o e-mail do Google já tem conta no TRINDATRON, as duas ficam ligadas e você passa a entrar pelo Google. Quando a opção não aparece, ela ainda não está disponível na sua versão.",
            "en": "Use \"Continue with Google\" on Sign in or Create account. If your Google e-mail already has a TRINDATRON account, they are linked and you sign in with Google from then on. If the option is not shown, it is not available in your version yet.",
            "es": "Usa \"Continuar con Google\" en Iniciar sesión o Crear cuenta. Si tu correo de Google ya tiene una cuenta de TRINDATRON, se vinculan y a partir de entonces entras con Google. Si la opción no aparece, todavía no está disponible en tu versión.",
        },
        ("google", "gmail", "conta google", "google account", "cuenta de google"),
    ),
    Article(
        "usage-windows", "plans",
        {"pt": "Como funciona o uso do plano", "en": "How plan usage works", "es": "Cómo funciona el uso del plan"},
        {
            "pt": "Seu plano tem uma sessão de 5 horas e um limite semanal. Em Uso do plano você vê quanto já usou, quanto resta e quando cada um renova. A semana reinicia no domingo ao meio-dia, no seu fuso horário.",
            "en": "Your plan has a 5-hour session and a weekly limit. In Plan usage you can see how much you used, what remains and when each renews. The week resets on Sunday at noon, in your time zone.",
            "es": "Tu plan tiene una sesión de 5 horas y un límite semanal. En Uso del plan ves cuánto usaste, cuánto queda y cuándo se renueva cada uno. La semana se reinicia el domingo al mediodía, en tu zona horaria.",
        },
        ("uso", "limite", "limit", "límite", "cota", "quota", "cuota", "semana", "week", "sessão", "renova", "reset", "acabou", "atingi"),
    ),
    Article(
        "limit-reached", "plans",
        {"pt": "Atingi o limite do meu plano", "en": "I reached my plan's limit", "es": "Alcancé el límite de mi plan"},
        {
            "pt": "Quando um limite é atingido, o TRINDATRON mostra quando o acesso renova. Você pode esperar a renovação ou escolher um plano com mais capacidade em Planos. Criações que falharam não contam no seu limite.",
            "en": "When a limit is reached, TRINDATRON shows when access renews. You can wait for the renewal or choose a plan with more capacity in Plans. Creations that failed do not count against your limit.",
            "es": "Cuando alcanzas un límite, TRINDATRON muestra cuándo se renueva el acceso. Puedes esperar la renovación o elegir un plan con más capacidad en Planes. Las creaciones que fallaron no cuentan en tu límite.",
        },
        ("limite", "limit", "límite", "bloqueado", "blocked", "atingi", "reached", "alcancé", "mais capacidade", "upgrade", "plano maior"),
    ),
    Article(
        "billing-cancel", "billing",
        {"pt": "Pagamentos e cancelamento", "en": "Payments and cancellation", "es": "Pagos y cancelación"},
        {
            "pt": "Planos são pagos por mês e o preço é sempre o mostrado em Planos. Para cancelar ou alterar uma assinatura recorrente, abra um chamado na categoria Cobrança: a equipe confirma o cancelamento pelo mesmo canal.",
            "en": "Plans are paid monthly and the price is always the one shown in Plans. To cancel or change a recurring subscription, open a ticket in the Billing category: the team confirms the cancellation there.",
            "es": "Los planes se pagan por mes y el precio es siempre el que aparece en Planes. Para cancelar o cambiar una suscripción recurrente, abre un ticket en la categoría Facturación: el equipo confirma la cancelación por ese canal.",
        },
        ("pagamento", "payment", "pago", "cancelar", "cancel", "assinatura", "subscription", "suscripción", "cobrança", "billing", "reembolso", "refund", "cartão", "card"),
    ),
    Article(
        "team-seats", "team",
        {"pt": "TEAM por assento", "en": "TEAM by seat", "es": "TEAM por asiento"},
        {
            "pt": "O TEAM é vendido por assento: Standard (capacidade do PRO) e Premium (capacidade do MAX), de 2 a 150 assentos. O dono compra os assentos, convida pessoas pelo e-mail e cada convite só pode ser aceito pela conta daquele e-mail. Quem já paga um plano individual precisa encerrá-lo antes de entrar em um time.",
            "en": "TEAM is sold by seat: Standard (PRO's capacity) and Premium (MAX's capacity), from 2 to 150 seats. The owner buys seats and invites people by e-mail, and each invitation can only be accepted by the account with that e-mail. Anyone already paying for an individual plan must end it before joining a team.",
            "es": "TEAM se vende por asiento: Standard (capacidad de PRO) y Premium (capacidad de MAX), de 2 a 150 asientos. El dueño compra los asientos e invita por correo, y cada invitación solo la puede aceptar la cuenta de ese correo. Quien ya paga un plan individual debe terminarlo antes de unirse a un equipo.",
        },
        ("team", "time", "equipe", "equipo", "assento", "seat", "asiento", "convite", "invite", "invitación", "membro", "member"),
    ),
    Article(
        "files-in-conversations", "files",
        {"pt": "Arquivos nas conversas", "en": "Files in conversations", "es": "Archivos en las conversaciones"},
        {
            "pt": "Anexe documentos a uma conversa e o TRINDATRON lê o conteúdo nas próximas respostas (texto e PDF, até 10 MB, 10 arquivos por conversa). Imagens ficam guardadas e listadas, mas o TRINDATRON ainda não analisa o conteúdo de imagens.",
            "en": "Attach documents to a conversation and TRINDATRON reads their content in its next answers (text and PDF, up to 10 MB, 10 files per conversation). Images are stored and listed, but TRINDATRON does not analyze image content yet.",
            "es": "Adjunta documentos a una conversación y TRINDATRON lee su contenido en las siguientes respuestas (texto y PDF, hasta 10 MB, 10 archivos por conversación). Las imágenes se guardan y se listan, pero TRINDATRON todavía no analiza su contenido.",
        },
        ("arquivo", "file", "archivo", "anexo", "attach", "adjuntar", "pdf", "documento", "document", "upload", "enviar"),
    ),
    Article(
        "creations", "creations",
        {"pt": "Criar imagens e vídeos", "en": "Creating images and videos", "es": "Crear imágenes y videos"},
        {
            "pt": "Quando disponível no seu plano, use + → Criar imagem ou Criar vídeo. Cada plano tem um número de criações por dia, mostrado em Uso do plano. A criação acontece em segundo plano e aparece na galeria; se falhar, nada é cobrado e não conta no limite do dia.",
            "en": "When your plan includes it, use + → Create image or Create video. Each plan has a number of creations per day, shown in Plan usage. Creation runs in the background and appears in your gallery; if it fails, nothing is charged and it does not count toward the daily limit.",
            "es": "Cuando tu plan lo incluye, usa + → Crear imagen o Crear video. Cada plan tiene un número de creaciones por día, visible en Uso del plan. La creación ocurre en segundo plano y aparece en tu galería; si falla, no se cobra y no cuenta en el límite diario.",
        },
        ("imagem", "image", "imagen", "vídeo", "video", "criar", "create", "crear", "gerar", "generate", "galeria", "gallery", "criação", "creation"),
    ),
    Article(
        "voice", "voice",
        {"pt": "Microfone e mensagens de voz", "en": "Microphone and voice messages", "es": "Micrófono y mensajes de voz"},
        {
            "pt": "Toque no microfone, fale e confirme: o TRINDATRON transcreve para o campo de texto. Se nada acontecer, permita o acesso ao microfone nas configurações do aparelho. Você pode descartar uma gravação antes de enviar.",
            "en": "Tap the microphone, speak and confirm: TRINDATRON types it into the text field. If nothing happens, allow microphone access in your device settings. You can discard a recording before sending.",
            "es": "Toca el micrófono, habla y confirma: TRINDATRON lo transcribe al campo de texto. Si no pasa nada, permite el acceso al micrófono en la configuración del dispositivo. Puedes descartar una grabación antes de enviar.",
        },
        ("microfone", "microphone", "micrófono", "voz", "voice", "áudio", "audio", "gravar", "record", "grabar", "transcrever"),
    ),
    Article(
        "privacy-files", "privacy",
        {"pt": "Privacidade dos seus arquivos", "en": "Privacy of your files", "es": "Privacidad de tus archivos"},
        {
            "pt": "Seus arquivos, anexos e criações só podem ser abertos pela sua conta. Links de download expiram em poucos minutos e criações são guardadas por 30 dias.",
            "en": "Your files, attachments and creations can only be opened by your account. Download links expire within minutes and creations are kept for 30 days.",
            "es": "Tus archivos, adjuntos y creaciones solo pueden abrirse con tu cuenta. Los enlaces de descarga caducan en pocos minutos y las creaciones se guardan durante 30 días.",
        },
        ("privacidade", "privacy", "privacidad", "dados", "data", "datos", "segurança", "security", "excluir", "delete", "borrar"),
    ),
)

CATEGORIES = tuple(dict.fromkeys(article.category for article in ARTICLES))


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(char for char in folded if unicodedata.category(char) != "Mn")


def language(value: str | None) -> str:
    code = (value or "pt").split("-")[0].lower()
    return code if code in LANGUAGES else "pt"


def score(article: Article, query: str, lang: str) -> int:
    words = [w for w in normalize(query).replace("?", " ").split() if len(w) > 2]
    if not words:
        return 0
    haystack_keywords = [normalize(k) for k in article.keywords]
    title = normalize(article.title[lang])
    body = normalize(article.body[lang])
    total = 0
    for word in words:
        if any(word in keyword or keyword in word for keyword in haystack_keywords):
            total += 3
        if word in title:
            total += 2
        if word in body:
            total += 1
    return total


def search(query: str, lang: str, *, category: str | None = None, limit: int = 5) -> list[Article]:
    pool = [a for a in ARTICLES if category is None or a.category == category]
    if not query.strip():
        return pool[:limit]
    ranked = sorted(((score(a, query, lang), a) for a in pool), key=lambda item: -item[0])
    return [article for points, article in ranked if points > 0][:limit]


def view(article: Article, lang: str) -> dict:
    return {"id": article.id, "category": article.category, "title": article.title[lang], "body": article.body[lang]}
