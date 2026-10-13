"""Render a MailMessage as an RFC 5322 message (for .eml and .mbox output)."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr

from .model import Address, MailMessage


def _addr_list(addresses: list[Address]) -> str:
    return ", ".join(formataddr((a.name, a.email)) for a in addresses)


def to_mime(msg: MailMessage) -> EmailMessage:
    out = EmailMessage()
    if msg.sender:
        out["From"] = _addr_list([msg.sender])
    if msg.to:
        out["To"] = _addr_list(msg.to)
    if msg.cc:
        out["Cc"] = _addr_list(msg.cc)
    if msg.bcc:
        out["Bcc"] = _addr_list(msg.bcc)
    if msg.reply_to:
        out["Reply-To"] = _addr_list(msg.reply_to)
    out["Subject"] = msg.subject
    when = msg.sent or msg.received or datetime.now(timezone.utc)
    out["Date"] = format_datetime(when)
    if msg.message_id:
        out["Message-ID"] = msg.message_id
    if msg.in_reply_to:
        out["In-Reply-To"] = msg.in_reply_to
    if msg.references:
        out["References"] = msg.references
    if msg.importance == 2:
        out["X-Priority"] = "1"
    elif msg.importance == 0:
        out["X-Priority"] = "5"
    if msg.categories:
        out["Keywords"] = ", ".join(msg.categories)

    out.set_content(msg.body_text or "")
    if msg.body_html:
        out.add_alternative(msg.body_html, subtype="html")
    for att in msg.attachments:
        maintype, _, subtype = (att.content_type or "application/octet-stream").partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        if att.inline and att.content_id and msg.body_html:
            html_part = out.get_body(("html",))
            if html_part is not None:
                html_part.add_related(att.data, maintype, subtype, cid=f"<{att.content_id}>",
                                      filename=att.filename, disposition="inline")
                continue
        out.add_attachment(att.data, maintype=maintype, subtype=subtype, filename=att.filename)
    return out


def to_bytes(msg: MailMessage) -> bytes:
    return to_mime(msg).as_bytes(policy=SMTP)
