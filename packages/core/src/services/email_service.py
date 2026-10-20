"""Transactional email: the account's own messages (confirm your email, reset
your password, your password changed, your account was locked).

Sent through Resend (https://resend.com/docs/api-reference/emails/send-email)
when settings.resend_api_key is set. Without it nothing is sent and each skipped
message is logged by kind only — never the recipient's full address, and never
the body, which carries a working link.

Delivery never fails the request that caused it: routes queue it as a
background task after the response, so a slow or failing provider cannot make
"forgot password" answer differently for an existing account than for an
unknown one.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass

import httpx

from src.config import settings
from src.connectors.email import EmailConnector, EmailConnectorError

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


@dataclass(frozen=True)
class Email:
    to: str
    #: What kind of message this is, for logs: "password_reset", "email_verify", ...
    kind: str
    subject: str
    text: str
    html: str


class ResendEmailConnector(EmailConnector):
    def __init__(self, api_key: str, sender: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._sender = sender
        self._client = client

    @property
    def connector_name(self) -> str:
        return "resend"

    async def is_available(self) -> bool:
        return bool(self._api_key and self._sender)

    async def send(self, *, to: str, subject: str, body: str, **kwargs) -> str:
        payload = {"from": self._sender, "to": [to], "subject": subject, "text": body}
        if kwargs.get("html"):
            payload["html"] = kwargs["html"]
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                resp = await self._client.post(RESEND_URL, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(RESEND_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmailConnectorError(f"resend unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise EmailConnectorError(f"resend refused the message: HTTP {resp.status_code}")
        return str(resp.json().get("id", ""))


_connector_override: EmailConnector | None = None


def set_connector(connector: EmailConnector | None) -> None:
    """Tests install a recording connector here; None restores the configured one."""
    global _connector_override
    _connector_override = connector


def _connector() -> EmailConnector | None:
    if _connector_override is not None:
        return _connector_override
    if settings.resend_api_key and settings.email_from:
        return ResendEmailConnector(settings.resend_api_key, settings.email_from)
    return None


def configured() -> bool:
    return _connector() is not None


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else "?"


async def deliver(email: Email) -> None:
    """Send one message. Never raises: a failure is logged, and the caller's answer is already sent."""
    connector = _connector()
    if connector is None:
        logger.warning("email: not sent, no provider configured | kind=%s to_domain=%s", email.kind, _domain(email.to))
        return
    try:
        await connector.send(to=email.to, subject=email.subject, body=email.text, html=email.html)
        logger.info("email: sent | kind=%s to_domain=%s", email.kind, _domain(email.to))
    except Exception as exc:  # noqa: BLE001 — delivery must never surface to the request
        logger.error("email: failed | kind=%s to_domain=%s error=%s", email.kind, _domain(email.to), exc)


# ── Messages ─────────────────────────────────────────────────────────────────

_FOOTER = "TRINDATRON · trindatron.com\nSe você não reconhece esta mensagem, ignore-a ou responda para nosso suporte."


def _html(paragraphs: list[str], link: tuple[str, str] | None = None) -> str:
    body = "".join(f'<p style="margin:0 0 16px">{html.escape(p)}</p>' for p in paragraphs)
    if link is not None:
        label, url = link
        body += (
            f'<p style="margin:24px 0"><a href="{html.escape(url, quote=True)}" '
            'style="background:#111;color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none;'
            f'display:inline-block">{html.escape(label)}</a></p>'
            f'<p style="margin:0 0 16px;font-size:12px;color:#666">Ou copie este endereço: {html.escape(url)}</p>'
        )
    footer = html.escape(_FOOTER).replace("\n", "<br>")
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#111;'
        f'max-width:520px;margin:0 auto;padding:24px">{body}'
        f'<hr style="border:none;border-top:1px solid #eee;margin:24px 0">'
        f'<p style="font-size:12px;color:#888;margin:0">{footer}</p></div>'
    )


def password_reset_email(to: str, link: str, minutes: int) -> Email:
    lines = [
        "Recebemos um pedido para redefinir a senha da sua conta TRINDATRON.",
        f"O link abaixo vale por {minutes} minutos e pode ser usado uma única vez.",
        "Se não foi você, ignore este e-mail: sua senha continua a mesma.",
    ]
    return Email(
        to=to, kind="password_reset", subject="Redefinir sua senha do TRINDATRON",
        text="\n\n".join([lines[0], lines[1], link, lines[2], _FOOTER]),
        html=_html(lines, ("Redefinir senha", link)),
    )


def email_verify_email(to: str, link: str, hours: int) -> Email:
    lines = [
        "Confirme que este é o seu e-mail para proteger sua conta TRINDATRON.",
        f"O link abaixo vale por {hours} horas.",
        "Se você não criou uma conta, ignore este e-mail.",
    ]
    return Email(
        to=to, kind="email_verify", subject="Confirme seu e-mail no TRINDATRON",
        text="\n\n".join([lines[0], lines[1], link, lines[2], _FOOTER]),
        html=_html(lines, ("Confirmar e-mail", link)),
    )


def password_changed_email(to: str, reset_link: str) -> Email:
    lines = [
        "A senha da sua conta TRINDATRON acabou de ser alterada, e todos os dispositivos foram desconectados.",
        "Se foi você, não precisa fazer nada.",
        "Se não foi você, redefina sua senha agora pelo link abaixo.",
    ]
    return Email(
        to=to, kind="password_changed", subject="Sua senha do TRINDATRON foi alterada",
        text="\n\n".join([*lines, reset_link, _FOOTER]),
        html=_html(lines, ("Redefinir senha", reset_link)),
    )


def account_locked_email(to: str, minutes: int, reset_link: str) -> Email:
    lines = [
        "Houve muitas tentativas de entrar na sua conta TRINDATRON com a senha errada.",
        f"Por segurança, o login com senha ficou bloqueado por {minutes} minutos.",
        "Se foi você, espere e tente de novo, ou redefina sua senha pelo link abaixo. "
        "Se não foi você, redefinir a senha é o mais seguro.",
    ]
    return Email(
        to=to, kind="account_locked", subject="Tentativas de acesso à sua conta TRINDATRON",
        text="\n\n".join([*lines, reset_link, _FOOTER]),
        html=_html(lines, ("Redefinir senha", reset_link)),
    )
