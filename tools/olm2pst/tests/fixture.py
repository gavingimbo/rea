"""Build synthetic OLM archives that mirror the Outlook for Mac export layout."""

from __future__ import annotations

import zipfile
from xml.sax.saxutils import escape, quoteattr

ACCOUNT = "Accounts/me@example.com"


def _addr(tag: str, people: list[tuple[str, str]]) -> str:
    inner = "".join(
        f"<emailAddress OPFContactEmailAddressAddress={quoteattr(a)} "
        f"OPFContactEmailAddressName={quoteattr(n)} OPFContactEmailAddressType=\"0\"/>"
        for n, a in people
    )
    return f"<{tag}>{inner}</{tag}>"


def email_xml(subject: str, *, sender=("Alice Example", "alice@example.com"),
              to=(("Me", "me@example.com"),), cc=(), html: str | None = None, text: str | None = None,
              sent="2024-03-01T09:15:00", received="2024-03-01T09:15:05", read=True,
              attachments: list[tuple[str, str, str, str]] = (), message_id="<m1@example.com>", categories=(), flagged=False) -> str:
    parts = [
        f"<OPFMessageCopySubject>{escape(subject)}</OPFMessageCopySubject>",
        _addr("OPFMessageCopySenderAddress", [sender]),
        _addr("OPFMessageCopyToAddresses", list(to)),
        _addr("OPFMessageCopyCCAddresses", list(cc)) if cc else "",
        f"<OPFMessageCopySentTime>{sent}</OPFMessageCopySentTime>",
        f"<OPFMessageCopyReceivedTime>{received}</OPFMessageCopyReceivedTime>",
        f"<OPFMessageCopyMessageID>{escape(message_id)}</OPFMessageCopyMessageID>",
        f"<OPFMessageGetIsRead>{1 if read else 0}</OPFMessageGetIsRead>",
    ]
    if categories:
        cats = "".join(f"<category OPFCategoryCopyName={quoteattr(c)}/>" for c in categories)
        parts.append(f"<OPFMessageCopyCategoryList>{cats}</OPFMessageCopyCategoryList>")
    if flagged:
        parts.append("<OPFMessageCopyGetFlagStatus>1</OPFMessageCopyGetFlagStatus>")
    if html is not None:
        parts.append(f"<OPFMessageCopyHTMLBody>{escape(html)}</OPFMessageCopyHTMLBody>")
    if text is not None:
        parts.append(f"<OPFMessageCopyBody>{escape(text)}</OPFMessageCopyBody>")
    if attachments:
        items = "".join(
            f"<messageAttachment OPFAttachmentName={quoteattr(n)} OPFAttachmentURL={quoteattr(u)} "
            f"OPFAttachmentContentType={quoteattr(ct)} OPFAttachmentContentID={quoteattr(cid)}/>"
            for n, u, ct, cid in attachments
        )
        parts.append(f"<OPFMessageCopyAttachmentList>{items}</OPFMessageCopyAttachmentList>")
    return "<email>" + "".join(parts) + "</email>"


def write_olm(path: str, folders: dict[str, list[str]], blobs: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, emails in folders.items():
            for i, e in enumerate(emails):
                name = f"{ACCOUNT}/com.microsoft.__Messages/{folder}/message_{i:05d}.xml"
                z.writestr(name, '<?xml version="1.0" encoding="UTF-8"?><emails>' + e + "</emails>")
        for name, data in (blobs or {}).items():
            z.writestr(name, data)
        z.writestr(f"{ACCOUNT}/com.microsoft.__Contacts/Contacts.xml", "<contacts/>")


def sample(path: str) -> None:
    big = bytes(range(256)) * 400  # 100 KiB attachment -> data tree with an XBLOCK
    small = b"%PDF-1.4 tiny"
    blobs = {
        f"{ACCOUNT}/com.microsoft.__Attachments/report.pdf": small,
        f"{ACCOUNT}/com.microsoft.__Attachments/big.bin": big,
        f"{ACCOUNT}/com.microsoft.__Attachments/logo.png": b"\x89PNG fake",
    }
    long_html = "<html><body>" + "<p>Line of text é ü 漢字</p>" * 800 + "</body></html>"
    folders = {
        "Inbox": [
            email_xml("Quarterly report", html="<p>Hello <b>there</b> é</p>", text="Hello there é",
                      cc=[("Bob", "bob@example.com")],
                      attachments=[("report.pdf", "com.microsoft.__Attachments/report.pdf", "application/pdf", "")]),
            email_xml("RE: Unread with big attachment", read=False, text="see attached", message_id="<m2@x>",
                      attachments=[("big.bin", "com.microsoft.__Attachments/big.bin", "application/octet-stream", "")]),
            email_xml("Long HTML body", html=long_html, message_id="<m3@x>"),
            email_xml("Inline image", html='<img src="cid:logo1">', message_id="<m4@x>",
                      attachments=[("logo.png", "com.microsoft.__Attachments/logo.png", "image/png", "logo1")]),
        ],
        "Inbox/Projects": [email_xml("Nested folder item", text="nested", message_id="<m5@x>",
                                        categories=["Work", "Important"], flagged=True)],
        "Sent Items": [email_xml("Sent one", sender=("Me", "me@example.com"),
                                 to=[("Alice Example", "alice@example.com")], text="sent", message_id="<m6@x>")],
        "Deleted Items": [email_xml("Trashed", text="bye", message_id="<m7@x>")],
    }
    write_olm(path, folders, blobs)


if __name__ == "__main__":
    import sys

    sample(sys.argv[1])
