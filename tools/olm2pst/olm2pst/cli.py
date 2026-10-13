"""Command line: olm2pst ARCHIVE.olm [-o OUTPUT] [--format pst|eml|mbox] [filters]."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone

from . import __version__
from .convert import FORMATS, Options, convert, scan
from .olm import OlmError

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*$", re.I)


def parse_size(text: str) -> int:
    match = _SIZE_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (examples: 500MB, 10GB)")
    number, unit = float(match.group(1)), match.group(2).upper()
    size = int(number * 1024 ** "_KMGT".index(unit or "_"))
    if size < 16 * 1024 * 1024:
        raise argparse.ArgumentTypeError("split size must be at least 16MB")
    return size


def parse_date(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date: {text!r} (use YYYY-MM-DD)") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="olm2pst",
        description="Convert an Outlook for Mac archive (.olm) to a PST file for Outlook on Windows, "
                    "or to EML / MBOX.",
    )
    parser.add_argument("source", nargs="?", help=".olm file (or an extracted OLM folder)")
    parser.add_argument("-o", "--output", help="output path (default: next to the source)")
    parser.add_argument("-f", "--format", choices=FORMATS, default="pst")
    parser.add_argument("--list", action="store_true", help="show the folders and message counts, then exit")
    parser.add_argument("--folder", action="append", default=[], metavar="PATH",
                        help="only convert this folder and its subfolders (repeatable), e.g. 'Inbox/Projects'")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATH",
                        help="skip this folder and its subfolders (repeatable)")
    parser.add_argument("--since", type=parse_date, help="only messages received on/after YYYY-MM-DD (UTC)")
    parser.add_argument("--until", type=parse_date, help="only messages received before YYYY-MM-DD (UTC)")
    parser.add_argument("--split", type=parse_size, metavar="SIZE",
                        help="PST only: start a new .pst file when one reaches SIZE (e.g. 10GB)")
    parser.add_argument("--name", help="display name of the PST in Outlook (default: source file name)")
    parser.add_argument("--keep-account-folder", action="store_true",
                        help="keep a top-level folder per account even when there is only one")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output")
    parser.add_argument("--gui", action="store_true", help="open the desktop window")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.gui or args.source is None:
        from .gui import run

        run(args.source)
        return 0

    try:
        if args.list:
            print_folders(args.source, args.keep_account_folder)
            return 0
        output = args.output or default_output(args.source, args.format)
        options = Options(include=args.folder, exclude=args.exclude, since=args.since, until=args.until,
                          split_bytes=args.split, keep_account_folder=args.keep_account_folder,
                          display_name=args.name, overwrite=args.overwrite)
        report = convert(args.source, output, args.format, options,
                         progress=_progress if sys.stderr.isatty() else None)
    except (OlmError, FileExistsError, OSError, ValueError) as exc:
        sys.stderr.write(f"\nolm2pst: error: {exc}\n")
        return 1
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    print("\n".join(report.lines()))
    return 2 if report.problems else 0


def _progress(done: int, total: int, folder: str) -> None:
    sys.stderr.write(f"\r[{done}/{total}] {folder[:60]:<60}")
    sys.stderr.flush()


def print_folders(source: str, keep_account_folder: bool) -> None:
    stats = scan(source, keep_account_folder)
    width = max((len("/".join(k)) for k in stats), default=10)
    print(f"{'Folder':<{width}}  {'Items':>7}  {'Unread':>6}  {'Attach':>6}  Date range")
    for key in sorted(stats):
        s = stats[key]
        span = f"{s.first:%Y-%m-%d} .. {s.last:%Y-%m-%d}" if s.first and s.last else "-"
        print(f"{'/'.join(key):<{width}}  {s.messages:>7}  {s.unread:>6}  {s.attachments:>6}  {span}")
    print(f"{'Total':<{width}}  {sum(s.messages for s in stats.values()):>7}")


def default_output(source: str, fmt: str) -> str:
    stem = os.path.splitext(source.rstrip("/\\"))[0]
    return stem + (".pst" if fmt == "pst" else f"-{fmt}")


if __name__ == "__main__":
    raise SystemExit(main())
