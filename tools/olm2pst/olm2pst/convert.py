"""Conversion pipeline shared by the command line and the desktop window."""

from __future__ import annotations

import mailbox
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator

from . import mime
from .model import MailMessage, Problem
from .olm import OlmReader, safe_name
from .pst.messaging import PstWriter

FORMATS = ("pst", "eml", "mbox")

Progress = Callable[[int, int, str], None]  # done, total message files, current folder

@dataclass
class Options:
    include: list[str] = field(default_factory=list)  # folder paths, e.g. "Inbox/Projects"
    exclude: list[str] = field(default_factory=list)
    since: datetime | None = None  # inclusive, on the received (else sent) time
    until: datetime | None = None  # exclusive
    split_bytes: int | None = None  # PST only: start a new file past this size
    keep_account_folder: bool = False
    display_name: str | None = None
    overwrite: bool = False

    def wants_folder(self, key: tuple[str, ...]) -> bool:
        path = "/".join(key).casefold()

        def under(prefixes: list[str]) -> bool:
            return any(path == p or path.startswith(p + "/")
                       for p in (x.strip("/").casefold() for x in prefixes))

        return (not self.include or under(self.include)) and not under(self.exclude)

    def wants_message(self, msg: MailMessage) -> bool:
        when = msg.received or msg.sent
        if when is None:
            return True  # undated items are kept; they are counted in the report
        if self.since and when < self.since:
            return False
        if self.until and when >= self.until:
            return False
        return True


@dataclass
class Report:
    outputs: list[str] = field(default_factory=list)
    messages: int = 0
    filtered_out: int = 0
    undated: int = 0
    folders: set[tuple[str, ...]] = field(default_factory=set)
    problems: list[Problem] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0

    def lines(self) -> list[str]:
        where = self.outputs[0] if len(self.outputs) == 1 else f"{len(self.outputs)} files"
        out = [f"Wrote {self.messages} messages in {len(self.folders)} folders to {where}"
               f" ({self.seconds:.1f}s)."]
        if len(self.outputs) > 1:
            out.extend(f"  {p}" for p in self.outputs)
        if self.filtered_out:
            out.append(f"Left out by the date filter: {self.filtered_out} messages.")
        if self.undated:
            out.append(f"Kept {self.undated} messages that have no date.")
        for kind, count in sorted(self.skipped.items()):
            out.append(f"Not converted: {count} {kind} file(s) (only mail is supported).")
        if self.problems:
            out.append(f"{len(self.problems)} problem(s):")
            out.extend(f"  {p.source}: {p.reason}" for p in self.problems[:50])
            if len(self.problems) > 50:
                out.append(f"  ... and {len(self.problems) - 50} more")
        return out


@dataclass
class FolderStats:
    messages: int = 0
    unread: int = 0
    attachments: int = 0
    first: datetime | None = None
    last: datetime | None = None


def folder_key(folder: tuple[str, ...], single_account: bool) -> tuple[str, ...]:
    return folder[1:] if single_account and len(folder) > 1 else folder


def scan(source: str, keep_account_folder: bool = False) -> dict[tuple[str, ...], FolderStats]:
    """Summarise an archive's folders without loading attachment data."""
    stats: dict[tuple[str, ...], FolderStats] = {}
    with OlmReader(source) as reader:
        single = len(reader.accounts()) <= 1 and not keep_account_folder
        for item in reader.items(load_content=False):
            s = stats.setdefault(folder_key(item.folder, single), FolderStats())
            m = item.message
            s.messages += 1
            s.unread += not m.is_read
            s.attachments += len(m.attachments)
            when = m.received or m.sent
            if when:
                s.first = min(s.first, when) if s.first else when
                s.last = max(s.last, when) if s.last else when
    return stats


def convert(source: str, output: str, fmt: str = "pst", options: Options | None = None, *,
            progress: Progress | None = None) -> Report:
    opts = options or Options()
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    if opts.split_bytes is not None and fmt != "pst":
        raise ValueError("splitting by size only applies to PST output")
    if os.path.exists(output) and not opts.overwrite:
        raise FileExistsError(f"{output} already exists (use overwrite to replace it)")
    started = time.monotonic()
    report = Report()
    with OlmReader(source) as reader:
        single = len(reader.accounts()) <= 1 and not opts.keep_account_folder
        name = opts.display_name or os.path.splitext(os.path.basename(source.rstrip("/\\")))[0]
        items = _items(reader, single, opts, report, progress)
        if fmt == "pst":
            _to_pst(items, output, name, opts.split_bytes, report)
        elif fmt == "eml":
            _to_eml(items, output, report)
        else:
            _to_mbox(items, output, report)
        report.problems.extend(reader.summary.problems)
        report.skipped = dict(reader.summary.skipped)
    report.seconds = time.monotonic() - started
    return report


def _items(reader: OlmReader, single: bool, opts: Options, report: Report,
           progress: Progress | None) -> Iterator[tuple[tuple[str, ...], MailMessage]]:
    total = len(reader.message_members)
    wanted_members = [m for m in reader.message_members
                      if opts.wants_folder(folder_key(reader.folder_of(m), single))]
    last_file = -1
    done_before = total - len(wanted_members)
    for item in reader.items(members=wanted_members):
        key = folder_key(item.folder, single)
        if progress and reader.summary.message_files != last_file:
            last_file = reader.summary.message_files
            progress(done_before + last_file, total, "/".join(key))
        if not opts.wants_message(item.message):
            report.filtered_out += 1
            continue
        if item.message.received is None and item.message.sent is None:
            report.undated += 1
        report.folders.add(key)
        report.messages += 1
        yield key, item.message


def split_name(output: str, part: int) -> str:
    if part == 1:
        return output
    stem, ext = os.path.splitext(output)
    return f"{stem} ({part}){ext or '.pst'}"


def _to_pst(items, output: str, name: str, split_bytes: int | None, report: Report) -> None:
    part = 0
    writer: PstWriter | None = None
    handle = None
    partial = ""

    def open_part() -> None:
        nonlocal part, writer, handle, partial
        part += 1
        partial = split_name(output, part) + ".partial"
        handle = open(partial, "w+b")
        label = name if part == 1 else f"{name} ({part})"
        writer = PstWriter(handle, label)

    def close_part() -> None:
        assert writer is not None and handle is not None
        writer.close()
        handle.close()
        final = partial[: -len(".partial")]
        os.replace(partial, final)
        report.outputs.append(final)

    try:
        open_part()
        for key, msg in items:
            assert writer is not None
            if split_bytes and writer.message_count:
                if writer.projected_size + _message_size(msg) > split_bytes:
                    close_part()
                    open_part()
            if len(key) == 1 and key[0].casefold() == "deleted items":
                folder = writer.deleted_items
            else:
                folder = writer.folder(key)
            if key and key[-1].casefold() == "drafts":
                msg.is_draft = True
            writer.add_message(folder, msg)
        close_part()
    except BaseException:
        if handle is not None and not handle.closed:
            handle.close()
        if partial and os.path.exists(partial):
            os.remove(partial)
        raise


def _message_size(msg: MailMessage) -> int:
    body = len(msg.body_text) * 2 + len(msg.body_html) * 2
    return 4096 + body + int(sum(len(a.data) for a in msg.attachments) * 1.02)


def _to_eml(items, output: str, report: Report) -> None:
    os.makedirs(output, exist_ok=True)
    counters: dict[tuple[str, ...], int] = {}
    for key, msg in items:
        directory = os.path.join(output, *(safe_name(p) for p in key))
        os.makedirs(directory, exist_ok=True)
        counters[key] = counters.get(key, 0) + 1
        stem = safe_name(msg.subject)[:80] or "no subject"
        path = os.path.join(directory, f"{counters[key]:06d} {stem}.eml")
        with open(path, "wb") as f:
            f.write(mime.to_bytes(msg))
    report.outputs.append(output)


def _to_mbox(items, output: str, report: Report) -> None:
    os.makedirs(output, exist_ok=True)
    boxes: dict[tuple[str, ...], mailbox.mbox] = {}
    try:
        for key, msg in items:
            box = boxes.get(key)
            if box is None:
                path = os.path.join(output, *(safe_name(p) for p in key)) + ".mbox"
                os.makedirs(os.path.dirname(path), exist_ok=True)
                box = boxes[key] = mailbox.mbox(path, create=True)
                box.lock()
            box.add(mime.to_mime(msg))
    finally:
        for box in boxes.values():
            box.flush()
            box.unlock()
            box.close()
    report.outputs.append(output)
