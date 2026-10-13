"""Large synthetic OLM: exercises multi-block heaps, BTH index levels, row-matrix
subnodes, SIBLOCKs, XXBLOCKs, multi-level NBT/BBT and FMap pages (>32 MiB)."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from fixture import ACCOUNT, email_xml, write_olm  # noqa: E402


def build(path: str, messages: int = 20000, folders: int = 300) -> None:
    inbox = [email_xml(f"Message {i} " + "x" * (i % 200), text=f"body {i}", read=i % 3 != 0,
                       message_id=f"<s{i}@x>") for i in range(messages)]
    tree = {"Inbox": inbox}
    for i in range(folders):
        tree[f"Archive/Year {i}"] = [email_xml(f"Old {i}", text="old", message_id=f"<o{i}@x>")]
    many = [(f"Person {i}", f"p{i}@example.com") for i in range(400)]
    tree["Big"] = [
        email_xml("Many recipients", to=many, text="hi all", message_id="<many@x>"),
        email_xml("Huge attachment", text="see file", message_id="<huge@x>",
                  attachments=[("huge.bin", "com.microsoft.__Attachments/huge.bin", "application/octet-stream", "")]),
        email_xml("Many attachments", text="lots", message_id="<lots@x>",
                  attachments=[(f"a{i}.txt", f"com.microsoft.__Attachments/a{i}.txt", "text/plain", "")
                               for i in range(400)]),
    ]
    blobs = {f"{ACCOUNT}/com.microsoft.__Attachments/huge.bin": os.urandom(20 * 1024 * 1024)}
    blobs.update({f"{ACCOUNT}/com.microsoft.__Attachments/a{i}.txt": f"file {i}".encode() for i in range(400)})
    write_olm(path, tree, blobs)


if __name__ == "__main__":
    build(sys.argv[1])
