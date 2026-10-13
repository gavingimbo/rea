"""Reader for Outlook for Mac archive (.olm) files.

An OLM file is a ZIP archive. Mail is stored as XML documents below
``.../com.microsoft.__Messages/<folder path>/`` with one ``<email>`` element per
message; attachment bytes are separate archive members referenced by
``OPFAttachmentURL``. An already-extracted archive directory is also accepted.
"""

from __future__ import annotations

import email
import email.policy
import os
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Iterator

from .model import Address, Attachment, MailMessage, Problem

MESSAGES_DIR = "com.microsoft.__Messages"
_OTHER_STORES = {
    "com.microsoft.__Calendars": "calendar items",
    "com.microsoft.__Contacts": "contacts",
    "com.microsoft.__Tasks": "tasks",
    "com.microsoft.__Notes": "notes",
}


class OlmError(Exception):
    """The input is not a readable OLM archive."""


@dataclass
class OlmItem:
    folder: tuple[str, ...]
    message: MailMessage
    position: tuple[str, int] = ("", 0)  # (archive member, index of <email> within it)


@dataclass
class OlmSummary:
    message_files: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)


class _Archive:
    """Uniform read access to a ZIP archive or an extracted directory."""

    def __init__(self, path: str):
        self.path = path
        if os.path.isdir(path):
            self._zip = None
            names = []
            for root, _, files in os.walk(path):
                for f in files:
                    rel = os.path.relpath(os.path.join(root, f), path)
                    names.append(rel.replace(os.sep, "/"))
            self.names = sorted(names)
        else:
            try:
                self._zip = zipfile.ZipFile(path)
            except (zipfile.BadZipFile, OSError) as exc:
                raise OlmError(f"{path} is not a readable OLM (ZIP) archive: {exc}") from exc
            self.names = sorted(n for n in self._zip.namelist() if not n.endswith("/"))
        self._by_lower = {n.lower(): n for n in self.names}
        self._by_basename: dict[str, list[str]] = {}
        for n in self.names:
            self._by_basename.setdefault(posixpath.basename(n).lower(), []).append(n)

    def open(self, name: str):
        if self._zip is not None:
            return self._zip.open(name)
        return open(os.path.join(self.path, *name.split("/")), "rb")

    def read(self, name: str) -> bytes:
        with self.open(name) as f:
            return f.read()

    def resolve(self, ref: str, base: str) -> str | None:
        """Find the member an in-archive reference (e.g. OPFAttachmentURL) points to."""
        ref = ref.strip().replace("\\", "/")
        if not ref:
            return None
        if ref.lower().startswith("file://"):
            ref = ref[7:]
        ref = ref.lstrip("/")
        candidates = [ref, posixpath.normpath(posixpath.join(base, ref))]
        parts = base.split("/")
        for i in range(len(parts)):
            candidates.append(posixpath.normpath(posixpath.join(*parts[: i + 1], ref)))
        for c in candidates:
            hit = self._by_lower.get(c.lower())
            if hit:
                return hit
        # Fall back to a unique suffix match.
        matches = [n for n in self._by_basename.get(posixpath.basename(ref).lower(), [])
                   if n.lower().endswith(ref.lower())]
        return matches[0] if len(matches) == 1 else None

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _text(elem: ET.Element, name: str) -> str:
    c = _child(elem, name)
    return (c.text or "") if c is not None else ""


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


_FRACTION_RE = re.compile(r"\.(\d+)")


def _parse_time(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    # Normalise for datetime.fromisoformat on Python < 3.11: "Z" suffix and
    # fractional seconds of any length.
    iso = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value.replace("Z", "+00:00"), count=1)
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # OLM stores UTC timestamps
    return parsed.astimezone(timezone.utc)


def _addresses(elem: ET.Element, name: str) -> list[Address]:
    c = _child(elem, name)
    if c is None:
        return []
    out = []
    for a in c.iter():
        attrs = {k.rsplit("}", 1)[-1]: v for k, v in a.attrib.items()}
        addr = attrs.get("OPFContactEmailAddressAddress", "").strip()
        nm = attrs.get("OPFContactEmailAddressName", "").strip()
        if addr or nm:
            out.append(Address(nm if nm != addr else "", addr))
    return out


def _importance(value: str) -> int:
    v = value.strip().lower()
    if v in ("1", "2", "high", "highest"):
        return 2
    if v in ("4", "5", "low", "lowest"):
        return 0
    return 1


def _folder_path(name: str) -> tuple[tuple[str, ...], str] | None:
    """Split a message XML member name into (folder path, account root)."""
    parts = name.split("/")
    try:
        i = parts.index(MESSAGES_DIR)
    except ValueError:
        return None
    folder = tuple(parts[i + 1:-1])
    account = parts[:i]
    if account[:1] == ["Accounts"] and len(account) > 1:
        label = account[1]
    elif account[:1] == ["Local"]:
        label = "On My Computer"
    else:
        label = "/".join(account) or "Archive"
    return (label,) + folder, "/".join(parts[:i])


def _apply_mime(msg: MailMessage, raw: bytes) -> None:
    """Fill gaps in ``msg`` from the original MIME source."""
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        header_end = raw.find(b"\n\n")
    if header_end > 0:
        msg.headers = raw[:header_end].decode("utf-8", "replace") + "\r\n"

    def addrs(field_name: str) -> list[Address]:
        values = parsed.get_all(field_name, [])
        return [Address(n, a) for n, a in getaddresses([str(v) for v in values]) if a or n]

    msg.subject = msg.subject or str(parsed.get("Subject", ""))
    if msg.sender is None:
        found = addrs("From")
        msg.sender = found[0] if found else None
    msg.to = msg.to or addrs("To")
    msg.cc = msg.cc or addrs("Cc")
    msg.bcc = msg.bcc or addrs("Bcc")
    msg.reply_to = msg.reply_to or addrs("Reply-To")
    msg.message_id = msg.message_id or str(parsed.get("Message-ID", "")).strip()
    msg.in_reply_to = msg.in_reply_to or str(parsed.get("In-Reply-To", "")).strip()
    msg.references = msg.references or " ".join(str(parsed.get("References", "")).split())
    if msg.sent is None and parsed.get("Date"):
        try:
            msg.sent = _parse_time(str(parsed["Date"]))
        except (TypeError, ValueError):
            pass
    if not msg.body_html and not msg.body_text:
        html_part = parsed.get_body(("html",))
        text_part = parsed.get_body(("plain",))
        if html_part is not None:
            msg.body_html = html_part.get_content()
        if text_part is not None:
            msg.body_text = text_part.get_content()
    if not msg.attachments:
        for part in parsed.iter_attachments():
            data = part.get_payload(decode=True) or b""
            cid = str(part.get("Content-ID", "")).strip().strip("<>")
            msg.attachments.append(Attachment(
                filename=part.get_filename() or "attachment",
                data=data,
                content_type=part.get_content_type(),
                content_id=cid,
                inline=part.get_content_disposition() == "inline",
            ))


def _parse_email(elem: ET.Element, archive: _Archive, member: str, account_root: str,
                 problems: list[Problem], load_content: bool = True) -> MailMessage:
    source = f"{member}"
    msg = MailMessage(source=source)
    msg.subject = _text(elem, "OPFMessageCopySubject")
    senders = _addresses(elem, "OPFMessageCopySenderAddress") or _addresses(elem, "OPFMessageCopyFromAddresses")
    msg.sender = senders[0] if senders else None
    msg.to = _addresses(elem, "OPFMessageCopyToAddresses")
    msg.cc = _addresses(elem, "OPFMessageCopyCCAddresses")
    msg.bcc = _addresses(elem, "OPFMessageCopyBCCAddresses")
    msg.reply_to = _addresses(elem, "OPFMessageCopyReplyToAddresses")
    msg.sent = _parse_time(_text(elem, "OPFMessageCopySentTime"))
    msg.received = _parse_time(_text(elem, "OPFMessageCopyReceivedTime"))
    msg.body_html = _text(elem, "OPFMessageCopyHTMLBody")
    msg.body_text = _text(elem, "OPFMessageCopyBody")
    msg.message_id = _text(elem, "OPFMessageCopyMessageID").strip()
    msg.in_reply_to = _text(elem, "OPFMessageCopyInReplyTo").strip()
    msg.references = " ".join(_text(elem, "OPFMessageCopyReferences").split())
    read = _text(elem, "OPFMessageGetIsRead")
    msg.is_read = _truthy(read) if read.strip() else True
    msg.importance = _importance(_text(elem, "OPFMessageGetPriority"))
    flag = _text(elem, "OPFMessageCopyGetFlagStatus") or _text(elem, "OPFMessageGetFlagStatus")
    msg.flagged = flag.strip() not in ("", "0")
    cats = _child(elem, "OPFMessageCopyCategoryList")
    if cats is not None:
        for c in cats.iter():
            name = c.attrib.get("OPFCategoryCopyName") or (c.text or "").strip()
            if name and _local(c.tag) != "OPFMessageCopyCategoryList":
                msg.categories.append(name)

    att_list = _child(elem, "OPFMessageCopyAttachmentList")
    base = posixpath.dirname(member)
    if att_list is not None:
        for a in att_list:
            attrs = {k.rsplit("}", 1)[-1]: v for k, v in a.attrib.items()}
            url = attrs.get("OPFAttachmentURL", "")
            name = attrs.get("OPFAttachmentName") or posixpath.basename(url) or "attachment"
            cid = attrs.get("OPFAttachmentContentID", "").strip().strip("<>")
            if not load_content:
                msg.attachments.append(Attachment(name, b"", attrs.get("OPFAttachmentContentType") or ""))
                continue
            target = archive.resolve(url, base) or archive.resolve(url, account_root)
            if target is None:
                problems.append(Problem(source, f"attachment '{name}' not found in archive ({url})"))
                continue
            msg.attachments.append(Attachment(
                filename=name,
                data=archive.read(target),
                content_type=attrs.get("OPFAttachmentContentType") or "application/octet-stream",
                content_id=cid,
                inline=bool(cid) and f"cid:{cid}" in msg.body_html,
            ))

    src_ref = _text(elem, "OPFMessageCopySource").strip()
    if src_ref and load_content:
        target = archive.resolve(src_ref, base) or archive.resolve(src_ref, account_root)
        if target is not None:
            try:
                _apply_mime(msg, archive.read(target))
            except Exception as exc:  # malformed MIME must not stop the conversion
                problems.append(Problem(source, f"could not parse MIME source: {exc}"))
    return msg


class OlmReader:
    """Iterates the mail messages of an OLM archive."""

    def __init__(self, path: str):
        self.path = path
        self._archive = _Archive(path)
        self.summary = OlmSummary()
        self.message_members = [n for n in self._archive.names
                                if n.lower().endswith(".xml") and _folder_path(n) is not None]
        for n in self._archive.names:
            for marker, label in _OTHER_STORES.items():
                if f"/{marker}/" in f"/{n}" and n.lower().endswith(".xml"):
                    self.summary.skipped[label] = self.summary.skipped.get(label, 0) + 1
        if not self.message_members and not self.summary.skipped:
            raise OlmError(f"{path} contains no Outlook for Mac mail data ({MESSAGES_DIR} not found)")

    def folder_of(self, member: str) -> tuple[str, ...]:
        """Folder path (account first) of a message XML member."""
        return _folder_path(member)[0]  # type: ignore[index]

    def accounts(self) -> list[str]:
        return sorted({self.folder_of(n)[0] for n in self.message_members})

    def __iter__(self) -> Iterator[OlmItem]:
        return self.items()

    def items(self, *, load_content: bool = True, members: list[str] | None = None) -> Iterator[OlmItem]:
        """Yield every message. ``load_content=False`` skips attachment bytes and
        MIME sources, for fast previews; ``OlmItem.position`` reloads one fully."""
        for member in self.message_members if members is None else members:
            folder, account_root = _folder_path(member)  # type: ignore[misc]
            self.summary.message_files += 1
            try:
                with self._archive.open(member) as f:
                    index = 0
                    for _, elem in ET.iterparse(f, events=("end",)):
                        if _local(elem.tag) != "email":
                            continue
                        msg = _parse_email(elem, self._archive, member, account_root,
                                           self.summary.problems, load_content)
                        yield OlmItem(folder, msg, (member, index))
                        index += 1
                        elem.clear()
            except ET.ParseError as exc:
                self.summary.problems.append(Problem(member, f"invalid XML: {exc}"))

    def load(self, position: tuple[str, int]) -> MailMessage:
        """Fully load the message at ``position`` (from a preview scan)."""
        member, wanted = position
        for item in self.items(members=[member]):
            if item.position[1] == wanted:
                return item.message
        raise KeyError(position)

    def close(self) -> None:
        self._archive.close()

    def __enter__(self) -> "OlmReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    """A file or folder name that is valid on Windows, macOS and Linux."""
    clean = _SAFE_RE.sub("_", name).strip(" .") or "_"
    if clean.split(".")[0].upper() in _RESERVED:
        clean = "_" + clean
    return clean
