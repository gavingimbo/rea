"""Messaging layer: message store, folders, messages ([MS-PST] section 2.4)."""

from __future__ import annotations

import html
import os
import re
import struct
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO

from ..model import Address, Attachment, MailMessage
from .ltp import (
    NodeData,
    SubnodeAllocator,
    TableLayout,
    build_pc,
    build_tc,
    layout,
    write_node,
)
from .ndb import (
    AMAP_INTERVAL,
    NID_TYPE_ASSOC_CONTENTS_TABLE,
    NID_TYPE_ATTACHMENT,
    NID_TYPE_CONTENTS_TABLE,
    NID_TYPE_HIERARCHY_TABLE,
    NID_TYPE_NORMAL_FOLDER,
    NID_TYPE_NORMAL_MESSAGE,
    NdbWriter,
    crc32,
    make_nid,
    nid_index,
)

NID_MESSAGE_STORE = 0x21
NID_NAME_TO_ID_MAP = 0x61
NID_ROOT_FOLDER = 0x122
NID_SEARCH_MANAGEMENT_QUEUE = 0x1E1
NID_SEARCH_ACTIVITY_LIST = 0x201
NID_SEARCH_DOMAIN_OBJECT = 0x261
NID_HIERARCHY_TABLE_TEMPLATE = 0x60D
NID_CONTENTS_TABLE_TEMPLATE = 0x60E
NID_ASSOC_CONTENTS_TABLE_TEMPLATE = 0x60F
NID_ATTACHMENT_TABLE = 0x671
NID_RECIPIENT_TABLE = 0x692

# Property tags used below.
PR_IMPORTANCE = 0x00170003
PR_MESSAGE_CLASS = 0x001A001F
PR_SENSITIVITY = 0x00360003
PR_SUBJECT = 0x0037001F
PR_CLIENT_SUBMIT_TIME = 0x00390040
PR_SENT_REPRESENTING_SEARCH_KEY = 0x003B0102
PR_SENT_REPRESENTING_ENTRYID = 0x00410102
PR_SENT_REPRESENTING_NAME = 0x0042001F
PR_MESSAGE_TO_ME = 0x0057000B
PR_MESSAGE_CC_ME = 0x0058000B
PR_SENT_REPRESENTING_ADDRTYPE = 0x0064001F
PR_SENT_REPRESENTING_EMAIL = 0x0065001F
PR_CONVERSATION_TOPIC = 0x0070001F
PR_TRANSPORT_HEADERS = 0x007D001F
PR_REPLY_RECIPIENT_NAMES = 0x0050001F
PR_RECIPIENT_TYPE = 0x0C150003
PR_SENDER_ENTRYID = 0x0C190102
PR_SENDER_NAME = 0x0C1A001F
PR_SENDER_SEARCH_KEY = 0x0C1D0102
PR_SENDER_ADDRTYPE = 0x0C1E001F
PR_SENDER_EMAIL = 0x0C1F001F
PR_DISPLAY_BCC = 0x0E02001F
PR_DISPLAY_CC = 0x0E03001F
PR_DISPLAY_TO = 0x0E04001F
PR_MESSAGE_DELIVERY_TIME = 0x0E060040
PR_MESSAGE_FLAGS = 0x0E070003
PR_MESSAGE_SIZE = 0x0E080003
PR_RESPONSIBILITY = 0x0E0F000B
PR_MESSAGE_STATUS = 0x0E170003
PR_ATTACH_SIZE = 0x0E200003
PR_RECORD_KEY = 0x0FF90102
PR_OBJECT_TYPE = 0x0FFE0003
PR_ENTRYID = 0x0FFF0102
PR_BODY = 0x1000001F
PR_HTML = 0x10130102
PR_INTERNET_MESSAGE_ID = 0x1035001F
PR_INTERNET_REFERENCES = 0x1039001F
PR_IN_REPLY_TO_ID = 0x1042001F
PR_ICON_INDEX = 0x10800003
PR_FLAG_STATUS = 0x10900003
PR_DISPLAY_NAME = 0x3001001F
PR_ADDRTYPE = 0x3002001F
PR_EMAIL_ADDRESS = 0x3003001F
PR_CREATION_TIME = 0x30070040
PR_LAST_MODIFICATION_TIME = 0x30080040
PR_SEARCH_KEY = 0x300B0102
PR_VALID_FOLDER_MASK = 0x35DF0003
PR_IPM_SUBTREE_ENTRYID = 0x35E00102
PR_IPM_WASTEBASKET_ENTRYID = 0x35E30102
PR_FINDER_ENTRYID = 0x35E70102
PR_CONTENT_COUNT = 0x36020003
PR_CONTENT_UNREAD = 0x36030003
PR_SUBFOLDERS = 0x360A000B
PR_CONTAINER_CLASS = 0x3613001F
PR_ATTACH_DATA_BIN = 0x37010102
PR_ATTACH_EXTENSION = 0x3703001F
PR_ATTACH_FILENAME = 0x3704001F
PR_ATTACH_METHOD = 0x37050003
PR_ATTACH_LONG_FILENAME = 0x3707001F
PR_RENDERING_POSITION = 0x370B0003
PR_ATTACH_MIME_TAG = 0x370E001F
PR_ATTACH_CONTENT_ID = 0x3712001F
PR_ATTACH_FLAGS = 0x37140003
PR_DISPLAY_TYPE = 0x39000003
PR_SMTP_ADDRESS = 0x39FE001F
PR_7BIT_DISPLAY_NAME = 0x39FF001F
PR_TRANSMITABLE_DISPLAY_NAME = 0x3A20001F
PR_SEND_RICH_INFO = 0x3A40000B
PR_INTERNET_CPID = 0x3FDE0003
PR_MESSAGE_CODEPAGE = 0x3FFD0003
PR_SENDER_SMTP_ADDRESS = 0x5D01001F
PR_SENT_REPRESENTING_SMTP_ADDRESS = 0x5D02001F
PR_RECIPIENT_ORDER = 0x5FDF0003
PR_RECIPIENT_DISPLAY_NAME = 0x5FF6001F
PR_RECIPIENT_FLAGS = 0x5FFD0003
PR_PST_PASSWORD = 0x67FF0003
PR_REPL_FLAGS = 0x0E380003
PR_ATTACHMENT_HIDDEN = 0x7FFE000B

# Named properties registered in the Name-to-ID map (section 2.4.7).
PS_PUBLIC_STRINGS = 2
PSETID_COMMON = uuid.UUID("00062008-0000-0000-C000-000000000046").bytes_le
NAMED_PROPERTIES = [
    # (wGuid index or GUID bytes, string name or numeric LID)
    (PS_PUBLIC_STRINGS, "Keywords"),  # categories, PtypMultipleString
    (PSETID_COMMON, 0x8530),  # PidLidFlagRequest
]
PR_CATEGORIES = 0x8000101F
PR_FLAG_REQUEST = 0x8001001F

MSGFLAG_READ = 0x0001
MSGFLAG_UNMODIFIED = 0x0002
MSGFLAG_UNSENT = 0x0008
MSGFLAG_HASATTACH = 0x0010
MSGFLAG_FROMME = 0x0020

ONE_OFF_PROVIDER = bytes.fromhex("812B1FA4BEA310199D6E00DD010F5402")

# Column layouts copied from Outlook-created tables in [MS-PST] section 3.12/3.13,
# as tag -> (ibData, cbData, iBit).
HIERARCHY_COLUMNS = {
    0x0E300003: (20, 4, 6), 0x0E330014: (24, 8, 7), 0x0E340102: (32, 4, 8),
    0x0E380003: (36, 4, 9), 0x3001001F: (8, 4, 2), 0x36020003: (12, 4, 3),
    0x36030003: (16, 4, 4), 0x360A000B: (52, 1, 5), 0x3613001F: (40, 4, 10),
    0x66350003: (44, 4, 11), 0x66360003: (48, 4, 12), 0x67F20003: (0, 4, 0),
    0x67F30003: (4, 4, 1),
}
CONTENTS_COLUMNS = {
    0x00170003: (20, 4, 5), 0x001A001F: (12, 4, 3), 0x00360003: (60, 4, 15),
    0x0037001F: (28, 4, 7), 0x00390040: (40, 8, 9), 0x0042001F: (24, 4, 6),
    0x0057000B: (116, 1, 13), 0x0058000B: (117, 1, 14), 0x0070001F: (68, 4, 17),
    0x00710102: (72, 4, 18), 0x0E03001F: (56, 4, 12), 0x0E04001F: (52, 4, 11),
    0x0E060040: (32, 8, 8), 0x0E070003: (16, 4, 4), 0x0E080003: (48, 4, 10),
    0x0E170003: (8, 4, 2), 0x0E300003: (88, 4, 21), 0x0E330014: (92, 8, 22),
    0x0E340102: (100, 4, 23), 0x0E380003: (112, 4, 26), 0x0E3C0102: (108, 4, 25),
    0x0E3D0102: (104, 4, 24), 0x10970003: (64, 4, 16), 0x30080040: (80, 8, 20),
    0x65C60003: (76, 4, 19), 0x67F20003: (0, 4, 0), 0x67F30003: (4, 4, 1),
}
ASSOC_COLUMNS = {
    0x001A001F: (12, 4, 3), 0x003A001F: (60, 4, 16), 0x0070001F: (56, 4, 15),
    0x0E070003: (16, 4, 4), 0x0E170003: (8, 4, 2), 0x3001001F: (20, 4, 5),
    0x67F20003: (0, 4, 0), 0x67F30003: (4, 4, 1), 0x6800001F: (44, 4, 11),
    0x6803000B: (64, 1, 12), 0x68051003: (48, 4, 13), 0x682F001F: (52, 4, 14),
    0x70030003: (24, 4, 6), 0x70040102: (28, 4, 7), 0x70050102: (32, 4, 8),
    0x7006001F: (36, 4, 9), 0x70070003: (40, 4, 10),
}
RECIPIENT_COLUMNS = {
    0x0C150003: (24, 4, 7), 0x0E0F000B: (108, 1, 2), 0x0FF90102: (32, 4, 9),
    0x0FFE0003: (36, 4, 10), 0x0FFF0102: (16, 4, 5), 0x3001001F: (20, 4, 6),
    0x3002001F: (8, 4, 3), 0x3003001F: (12, 4, 4), 0x300B0102: (28, 4, 8),
    0x39000003: (40, 4, 11), 0x39050003: (48, 4, 14), 0x39FE001F: (52, 4, 15),
    0x39FF001F: (44, 4, 13), 0x3A00001F: (56, 4, 16), 0x3A20001F: (60, 4, 17),
    0x3A40000B: (109, 1, 12), 0x5FDE0003: (64, 4, 18), 0x5FDF0003: (68, 4, 19),
    0x5FEB0003: (76, 4, 21), 0x5FEF0003: (80, 4, 22), 0x5FF20003: (84, 4, 23),
    0x5FF50003: (88, 4, 24), 0x5FF6001F: (92, 4, 25), 0x5FF70102: (96, 4, 26),
    0x5FFD0003: (100, 4, 27), 0x5FFF0003: (104, 4, 28), 0x67F20003: (0, 4, 0),
    0x67F30003: (4, 4, 1),
}
ATTACHMENT_TAGS = [
    PR_ATTACH_SIZE, PR_ATTACH_FILENAME, PR_ATTACH_METHOD, PR_RENDERING_POSITION,
    PR_ATTACH_LONG_FILENAME, PR_ATTACH_MIME_TAG, PR_ATTACHMENT_HIDDEN,
]

HIERARCHY_LAYOUT = layout(HIERARCHY_COLUMNS, HIERARCHY_COLUMNS)
CONTENTS_LAYOUT = layout(CONTENTS_COLUMNS, CONTENTS_COLUMNS)
ASSOC_LAYOUT = layout(ASSOC_COLUMNS, ASSOC_COLUMNS)
RECIPIENT_LAYOUT = layout(RECIPIENT_COLUMNS, RECIPIENT_COLUMNS)
ATTACHMENT_LAYOUT = layout(ATTACHMENT_TAGS)

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def filetime(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value - _FILETIME_EPOCH
    return (delta.days * 86400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


def one_off_entry_id(address: Address) -> bytes:
    """One-off EntryID for an SMTP address ([MS-OXCDATA] 2.2.5.1), Unicode form."""
    name = address.display()
    return (
        b"\0\0\0\0" + ONE_OFF_PROVIDER + b"\0\0" + b"\x01\x90"
        + (name + "\0").encode("utf-16-le")
        + "SMTP\0".encode("utf-16-le")
        + (address.email + "\0").encode("utf-16-le")
    )


def search_key(address: Address) -> bytes:
    return ("SMTP:" + address.email.upper()).encode("ascii", "replace") + b"\0"


_TAG_RE = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)


def html_to_text(markup: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", markup, flags=re.I)
    text = html.unescape(_TAG_RE.sub("", text))
    text = re.sub(r"[ \t\r\f\v]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _short_filename(name: str) -> str:
    base, ext = os.path.splitext(name)
    clean = re.sub(r"[^A-Za-z0-9_-]", "", base)[:8] or "ATTACH"
    return clean.upper() + ext[:4].upper()


@dataclass
class Folder:
    nid: int
    name: str
    parent: "Folder | None"
    container_class: str | None = "IPF.Note"
    children: list["Folder"] = field(default_factory=list)
    rows: list[tuple[int, dict[int, object]]] = field(default_factory=list)
    unread: int = 0

    def child(self, name: str) -> "Folder | None":
        for c in self.children:
            if c.name.casefold() == name.casefold():
                return c
        return None


class PstWriter:
    """Writes a new Unicode PST file containing mail folders and messages."""

    def __init__(self, stream: BinaryIO, display_name: str = "Personal Folders"):
        self._ndb = NdbWriter(stream)
        self._uid = uuid.uuid4().bytes
        self._display_name = display_name
        self._next_folder = 0x400
        self._next_message = 0x10000
        self.root = Folder(NID_ROOT_FOLDER, "", None, None)
        self.ipm_subtree = self._new_folder("Top of Personal Folders", self.root)
        self.search_root = self._new_folder("Search Root", self.root, container_class=None)
        self.deleted_items = self._new_folder("Deleted Items", self.ipm_subtree)
        self.message_count = 0
        self._table_bytes = 0

    # -- folders ----------------------------------------------------------

    def _new_folder(self, name: str, parent: Folder, container_class: str | None = "IPF.Note") -> Folder:
        self._next_folder += 1
        folder = Folder(make_nid(NID_TYPE_NORMAL_FOLDER, self._next_folder), name, parent, container_class)
        parent.children.append(folder)
        return folder

    def folder(self, path: tuple[str, ...] | list[str]) -> Folder:
        """Return the folder at ``path`` below the top of the store, creating it if needed."""
        current = self.ipm_subtree
        for part in path:
            name = part.strip() or "Untitled"
            found = current.child(name)
            current = found if found is not None else self._new_folder(name, current)
        return current

    @property
    def projected_size(self) -> int:
        """Upper estimate of the file size if the PST were closed now.

        Covers the folder tables (rows plus their strings), BTree pages for
        every block and node, and rounding up to a whole AMap interval.
        """
        btree = self._ndb.block_count * 512 // 18 + self._ndb.node_count * 512 // 13
        return self._ndb.bytes_written + self._table_bytes + btree + 2 * AMAP_INTERVAL

    def entry_id(self, nid: int) -> bytes:
        return b"\0\0\0\0" + self._uid + nid.to_bytes(4, "little")

    # -- messages ---------------------------------------------------------

    def add_message(self, folder: Folder, msg: MailMessage) -> None:
        self._next_message += 1
        nid = make_nid(NID_TYPE_NORMAL_MESSAGE, self._next_message)
        props, row = self._message_props(msg)
        subnodes = SubnodeAllocator()
        subnodes.add(build_tc(RECIPIENT_LAYOUT, self._recipient_rows(msg)), nid=NID_RECIPIENT_TABLE)
        if msg.attachments:
            att_rows = []
            for position, att in enumerate(msg.attachments):
                att_props = self._attachment_props(att)
                att_nid = subnodes.add(build_pc(att_props), NID_TYPE_ATTACHMENT)
                att_rows.append((att_nid, {t: att_props.get(t) for t in ATTACHMENT_TAGS}))
            subnodes.add(build_tc(ATTACHMENT_LAYOUT, att_rows), nid=NID_ATTACHMENT_TABLE)
        node = build_pc(props, subnodes)
        node.subnodes = subnodes.subnodes
        bid_data, bid_sub = write_node(self._ndb, node)
        self._ndb.add_node(nid, bid_data, bid_sub, folder.nid)
        folder.rows.append((nid, row))
        self._table_bytes += CONTENTS_LAYOUT.row_size + 32 + sum(
            len(v) * 2 for v in row.values() if isinstance(v, str)) + sum(
            len(v) for v in row.values() if isinstance(v, bytes))
        if not msg.is_read:
            folder.unread += 1
        self.message_count += 1

    def _message_props(self, msg: MailMessage) -> tuple[dict[int, object], dict[int, object]]:
        now = datetime.now(timezone.utc)
        sent = msg.sent or msg.received or now
        received = msg.received or msg.sent or now
        flags = MSGFLAG_UNMODIFIED
        if msg.is_read:
            flags |= MSGFLAG_READ
        if msg.attachments:
            flags |= MSGFLAG_HASATTACH
        if msg.is_draft:
            flags |= MSGFLAG_UNSENT
        body_text = msg.body_text or (html_to_text(msg.body_html) if msg.body_html else "")
        html_bytes = msg.body_html.encode("utf-8") if msg.body_html else None
        size = 512 + len(body_text) * 2 + len(html_bytes or b"") + sum(len(a.data) for a in msg.attachments)
        subject = msg.subject.replace("\x01", "")
        props: dict[int, object] = {
            PR_MESSAGE_CLASS: "IPM.Note",
            PR_SUBJECT: subject,
            PR_CONVERSATION_TOPIC: _normalized_subject(subject),
            PR_IMPORTANCE: msg.importance,
            PR_SENSITIVITY: 0,
            PR_MESSAGE_FLAGS: flags,
            PR_MESSAGE_STATUS: 0,
            PR_MESSAGE_SIZE: min(size, 0x7FFFFFFF),
            PR_CLIENT_SUBMIT_TIME: filetime(sent),
            PR_MESSAGE_DELIVERY_TIME: filetime(received),
            PR_CREATION_TIME: filetime(received),
            PR_LAST_MODIFICATION_TIME: filetime(received),
            PR_SEARCH_KEY: uuid.uuid4().bytes,
            PR_MESSAGE_TO_ME: False,
            PR_MESSAGE_CC_ME: False,
            PR_DISPLAY_TO: "; ".join(a.display() for a in msg.to),
            PR_DISPLAY_CC: "; ".join(a.display() for a in msg.cc),
            PR_DISPLAY_BCC: "; ".join(a.display() for a in msg.bcc),
            PR_BODY: body_text,
            PR_INTERNET_CPID: 65001,
            PR_MESSAGE_CODEPAGE: 65001,
            PR_ICON_INDEX: 0xFFFFFFFF,
        }
        if html_bytes is not None:
            props[PR_HTML] = html_bytes
        if msg.sender is not None:
            s = msg.sender
            entry = one_off_entry_id(s)
            props.update({
                PR_SENDER_NAME: s.display(), PR_SENDER_EMAIL: s.email, PR_SENDER_ADDRTYPE: "SMTP",
                PR_SENDER_SMTP_ADDRESS: s.email, PR_SENDER_ENTRYID: entry, PR_SENDER_SEARCH_KEY: search_key(s),
                PR_SENT_REPRESENTING_NAME: s.display(), PR_SENT_REPRESENTING_EMAIL: s.email,
                PR_SENT_REPRESENTING_ADDRTYPE: "SMTP", PR_SENT_REPRESENTING_SMTP_ADDRESS: s.email,
                PR_SENT_REPRESENTING_ENTRYID: entry, PR_SENT_REPRESENTING_SEARCH_KEY: search_key(s),
            })
        if msg.reply_to:
            props[PR_REPLY_RECIPIENT_NAMES] = "; ".join(a.display() for a in msg.reply_to)
        if msg.message_id:
            props[PR_INTERNET_MESSAGE_ID] = msg.message_id
        if msg.in_reply_to:
            props[PR_IN_REPLY_TO_ID] = msg.in_reply_to
        if msg.references:
            props[PR_INTERNET_REFERENCES] = msg.references
        if msg.flagged:
            props[PR_FLAG_STATUS] = 2  # followupFlagged
            props[PR_FLAG_REQUEST] = "Follow up"
        if msg.categories:
            props[PR_CATEGORIES] = list(msg.categories)
        props[PR_TRANSPORT_HEADERS] = msg.headers or _synthesize_headers(msg, sent)
        row = {tag: props.get(tag) for tag in CONTENTS_COLUMNS}
        return props, row

    @staticmethod
    def _recipient_rows(msg: MailMessage) -> list[tuple[int, dict[int, object]]]:
        rows = []
        order = 0
        for rtype, addresses in ((1, msg.to), (2, msg.cc), (3, msg.bcc)):
            for a in addresses:
                name = a.display()
                rows.append((order, {
                    PR_RECIPIENT_TYPE: rtype, PR_RESPONSIBILITY: True, PR_OBJECT_TYPE: 6,
                    PR_ENTRYID: one_off_entry_id(a), PR_DISPLAY_NAME: name, PR_ADDRTYPE: "SMTP",
                    PR_EMAIL_ADDRESS: a.email, PR_SEARCH_KEY: search_key(a), PR_DISPLAY_TYPE: 0,
                    PR_SMTP_ADDRESS: a.email, PR_7BIT_DISPLAY_NAME: name,
                    PR_TRANSMITABLE_DISPLAY_NAME: name, PR_SEND_RICH_INFO: False,
                    PR_RECIPIENT_ORDER: order, PR_RECIPIENT_FLAGS: 1,
                    PR_RECIPIENT_DISPLAY_NAME: name,
                }))
                order += 1
        return rows

    @staticmethod
    def _attachment_props(att: Attachment) -> dict[int, object]:
        name = att.filename or "attachment"
        props: dict[int, object] = {
            PR_ATTACH_METHOD: 1,  # ATTACH_BY_VALUE
            PR_RENDERING_POSITION: 0xFFFFFFFF,
            PR_ATTACH_SIZE: min(len(att.data) + 256, 0x7FFFFFFF),
            PR_ATTACH_FILENAME: _short_filename(name),
            PR_ATTACH_LONG_FILENAME: name,
            PR_DISPLAY_NAME: name,
            PR_ATTACH_EXTENSION: os.path.splitext(name)[1],
            PR_ATTACH_MIME_TAG: att.content_type,
            PR_OBJECT_TYPE: 7,  # MAPI_ATTACH
            PR_ATTACH_DATA_BIN: att.data,
        }
        if att.content_id:
            props[PR_ATTACH_CONTENT_ID] = att.content_id
        if att.inline and att.content_id:
            props[PR_ATTACH_FLAGS] = 4  # ATT_MHTML_REF
            props[PR_ATTACHMENT_HIDDEN] = True
        return props

    # -- finishing --------------------------------------------------------

    def close(self) -> None:
        ndb = self._ndb
        self._write_folder(self.root, NID_ROOT_FOLDER)
        empty = [
            (NID_HIERARCHY_TABLE_TEMPLATE, HIERARCHY_LAYOUT),
            (NID_CONTENTS_TABLE_TEMPLATE, CONTENTS_LAYOUT),
            (NID_ASSOC_CONTENTS_TABLE_TEMPLATE, ASSOC_LAYOUT),
            (NID_ATTACHMENT_TABLE, ATTACHMENT_LAYOUT),
            (NID_RECIPIENT_TABLE, RECIPIENT_LAYOUT),
        ]
        for nid, table in empty:
            ndb.add_node(nid, *write_node(ndb, build_tc(table, [])))
        ndb.add_node(NID_NAME_TO_ID_MAP, *write_node(ndb, build_pc(name_to_id_map(NAMED_PROPERTIES))))
        store = {
            PR_RECORD_KEY: self._uid,
            PR_DISPLAY_NAME: self._display_name,
            PR_IPM_SUBTREE_ENTRYID: self.entry_id(self.ipm_subtree.nid),
            PR_IPM_WASTEBASKET_ENTRYID: self.entry_id(self.deleted_items.nid),
            PR_FINDER_ENTRYID: self.entry_id(self.search_root.nid),
            PR_VALID_FOLDER_MASK: 0x89,
            PR_PST_PASSWORD: 0,
            PR_REPL_FLAGS: 0,
        }
        ndb.add_node(NID_MESSAGE_STORE, *write_node(ndb, build_pc(store)))
        for nid in (NID_SEARCH_MANAGEMENT_QUEUE, NID_SEARCH_ACTIVITY_LIST, NID_SEARCH_DOMAIN_OBJECT):
            ndb.add_node(nid, 0, 0, 0)
        ndb.finish()

    def _folder_props(self, folder: Folder) -> dict[int, object]:
        props: dict[int, object] = {
            PR_DISPLAY_NAME: folder.name,
            PR_CONTENT_COUNT: len(folder.rows),
            PR_CONTENT_UNREAD: folder.unread,
            PR_SUBFOLDERS: bool(folder.children),
        }
        if folder.container_class:
            props[PR_CONTAINER_CLASS] = folder.container_class
        return props

    def _write_folder(self, folder: Folder, parent_nid: int) -> None:
        ndb = self._ndb
        index = nid_index(folder.nid)
        for child in folder.children:
            self._write_folder(child, folder.nid)
        ndb.add_node(folder.nid, *write_node(ndb, build_pc(self._folder_props(folder))), parent_nid)
        hierarchy = [(c.nid, {t: v for t, v in self._folder_props(c).items() if t in HIERARCHY_COLUMNS})
                     for c in folder.children]
        tables = [
            (NID_TYPE_HIERARCHY_TABLE, HIERARCHY_LAYOUT, hierarchy),
            (NID_TYPE_CONTENTS_TABLE, CONTENTS_LAYOUT, folder.rows),
            (NID_TYPE_ASSOC_CONTENTS_TABLE, ASSOC_LAYOUT, []),
        ]
        for nid_type_, table, rows in tables:
            ndb.add_node(make_nid(nid_type_, index), *write_node(ndb, build_tc(table, rows)))
        folder.rows = []  # release memory


def name_to_id_map(names: list[tuple[int | bytes, str | int]], buckets: int = 251) -> dict[int, object]:
    """Build the Name-to-ID map PC; NPIDs are assigned 0x8000 + list position."""
    guids: list[bytes] = []
    entries = bytearray()
    strings = bytearray()
    hashed: dict[int, bytearray] = {}
    for index, (guid, name) in enumerate(names):
        if isinstance(guid, bytes):
            if guid not in guids:
                guids.append(guid)
            wguid = 3 + guids.index(guid)
        else:
            wguid = guid
        if isinstance(name, str):
            encoded = name.encode("utf-16-le")
            prop_id = len(strings)
            strings += struct.pack("<I", len(encoded)) + encoded
            strings += b"\0" * (-len(strings) % 4)
            hash_id = crc32(encoded)
            kind = 1
        else:
            prop_id = hash_id = name
            kind = 0
        guid_kind = (wguid << 1) | kind
        entries += struct.pack("<IHH", prop_id, guid_kind, index)
        bucket = (hash_id ^ guid_kind) % buckets
        hashed.setdefault(bucket, bytearray()).extend(struct.pack("<IHH", hash_id, guid_kind, index))
    props: dict[int, object] = {
        0x00010003: buckets,
        0x00020102: b"".join(guids),
        0x00030102: bytes(entries),
        0x00040102: bytes(strings),
    }
    for bucket, data in hashed.items():
        props[((0x1000 + bucket) << 16) | 0x0102] = bytes(data)
    return props


_PREFIX_RE = re.compile(r"^([^\s\d:]{1,3}):\s*")


def _normalized_subject(subject: str) -> str:
    match = _PREFIX_RE.match(subject)
    return subject[match.end():] if match else subject


def _format_addresses(addresses: list[Address]) -> str:
    from email.utils import formataddr

    return ", ".join(formataddr((a.name, a.email)) for a in addresses)


def _synthesize_headers(msg: MailMessage, sent: datetime) -> str:
    from email.utils import format_datetime

    lines = []
    if msg.sender:
        lines.append("From: " + _format_addresses([msg.sender]))
    if msg.to:
        lines.append("To: " + _format_addresses(msg.to))
    if msg.cc:
        lines.append("Cc: " + _format_addresses(msg.cc))
    lines.append("Subject: " + msg.subject)
    lines.append("Date: " + format_datetime(sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)))
    if msg.message_id:
        lines.append("Message-ID: " + msg.message_id)
    if msg.in_reply_to:
        lines.append("In-Reply-To: " + msg.in_reply_to)
    lines.append("MIME-Version: 1.0")
    return "\r\n".join(lines) + "\r\n"
