"""Format-neutral mail model shared by the OLM reader and the writers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Address:
    name: str
    email: str

    def display(self) -> str:
        return self.name or self.email


@dataclass
class Attachment:
    filename: str
    data: bytes
    content_type: str = "application/octet-stream"
    content_id: str = ""
    inline: bool = False


@dataclass
class MailMessage:
    subject: str = ""
    sender: Address | None = None
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    bcc: list[Address] = field(default_factory=list)
    reply_to: list[Address] = field(default_factory=list)
    sent: datetime | None = None  # timezone-aware, UTC
    received: datetime | None = None
    body_text: str = ""
    body_html: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    is_read: bool = True
    importance: int = 1  # 0 low, 1 normal, 2 high
    flagged: bool = False
    categories: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    headers: str = ""  # original Internet headers, when known
    is_draft: bool = False
    source: str = ""  # location of the item inside the OLM archive


@dataclass
class Problem:
    """A recoverable issue found while reading or converting an item."""

    source: str
    reason: str
