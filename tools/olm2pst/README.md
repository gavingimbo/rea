# olm2pst

Convert an **Outlook for Mac archive (`.olm`)** into a **PST file** that Outlook on
Windows can open or import. It can also export to EML files or MBOX mailboxes.

- Pure Python standard library: no dependencies and no network access.
- Runs locally on macOS, Windows, or Linux with Python 3.9 or newer.
- The PST writer is a clean-room implementation of Microsoft's published
  [MS-PST] file format specification. It is not derived from any commercial converter.

## On Windows

Pick whichever is easiest:

1. **Standalone app, nothing to install.** Download `olm2pst-windows` from this
   repository's **Actions → olm2pst Windows build** run (latest run → Artifacts),
   then unzip it.
   - Double-click **`OLM2PST.exe`** for the window, or drag an `.olm` file onto it.
   - **`olm2pst.exe`** is the command-line version, e.g. `olm2pst.exe Archive.olm`.

   The exes are unsigned, so Windows SmartScreen may warn. Choose
   **More info → Run anyway**.
2. **With Python.** Install Python 3.9+ from python.org (or run
   `winget install Python.Python.3.12`), then double-click
   **`windows\OLM2PST.bat`**. You can also drag an `.olm` file onto it.
3. **With Docker Desktop** (command line only, no window). In the folder that holds the archive:

   ```bat
   docker build -t olm2pst path\to\tools\olm2pst
   docker run --rm -v "%cd%:/data" olm2pst /data/Archive.olm --list
   docker run --rm -v "%cd%:/data" olm2pst /data/Archive.olm
   ```

   `Archive.pst` is written next to the `.olm`. In PowerShell, use `"${PWD}:/data"`.

## Quick start (any OS)

From this folder (`tools/olm2pst`):

```sh
# Desktop window: preview the archive, pick folders and dates, then convert
python3 -m olm2pst --gui

# Command line
python3 -m olm2pst ~/Desktop/Archive.olm                 # -> ~/Desktop/Archive.pst
python3 -m olm2pst Archive.olm --list                    # folders, counts, date ranges
python3 -m olm2pst Archive.olm -o Work.pst --folder Inbox --exclude "Inbox/Newsletters"
python3 -m olm2pst Archive.olm --since 2022-01-01 --until 2024-01-01
python3 -m olm2pst Archive.olm --split 10GB              # Archive.pst, Archive (2).pst, ...
python3 -m olm2pst Archive.olm -f eml -o Archive-eml     # or -f mbox
```

Alternatively, run `pip install .` to get an `olm2pst` command.

If the window doesn't open on macOS, use the Python from python.org, which
includes Tk. You can also use the command line, which needs nothing extra.

## Getting the PST into Outlook on Windows

1. Copy the `.pst` file to the Windows PC.
2. Outlook → **File → Open & Export → Open Outlook Data File** and choose the
   `.pst`. It appears as a separate mailbox named after the archive.
   To merge into your main mailbox instead, use **File → Open & Export →
   Import/Export → Import from another program or file → Outlook Data File (.pst)**.

## What is converted

| Item | Status |
|---|---|
| Folder tree (all accounts, nested folders) | ✅ "Deleted Items" maps to Outlook's Deleted Items |
| Subject, from, to/cc/bcc, reply-to, sent/received dates | ✅ |
| Plain-text and HTML bodies (Unicode) | ✅ |
| Attachments, including inline images (`cid:`) | ✅ |
| Read/unread, importance, follow-up flag, categories | ✅ |
| Message-ID, In-Reply-To, References, Internet headers | ✅ |
| Contacts, calendar, tasks, notes | ❌ Not yet; counted and reported as "not converted" |

## Features compared with Stellar Converter for OLM

These are from Stellar's public product documentation:

| Stellar feature | olm2pst |
|---|---|
| Three-pane preview before saving | ✅ `--gui` (folders, message list, message and attachments) |
| Save as PST / EML | ✅ |
| Save as MSG / RTF / HTML / PDF | ❌ (MBOX instead) |
| Filters for selective saving | ✅ Folder include/exclude and date range |
| Auto-split large PST | ✅ `--split SIZE` (off by default) |
| Export into a live Outlook profile / Microsoft 365 | ❌ Open or import the PST in Outlook instead |
| Contacts, calendar, tasks, notes | ❌ Mail only, for now |

## How it was verified

The tests in `tests/` build synthetic OLM archives with the same layout as an
Outlook for Mac export, convert them, and check the result three ways:

- `tests/verify_pst.py` is an independent structural checker for the MS-PST
  rules Outlook depends on: header and page CRCs, block and page signatures,
  allocation maps matching every allocation exactly (no leaks or overlaps),
  B-tree ordering and keys, block reference counts, heap page maps, folder and
  table coherence, and full-size row-matrix blocks.
- **libpff** (`pip install libpff-python`) reads every folder, message,
  recipient and attachment. Attachment bytes are compared by hash.
- **libpst** (`readpst`) extracts every message with identical content.

The stress test has 20,303 messages in 302 folders, a message with 400
recipients, a message with 400 attachments, and a 20 MB attachment, producing a
92 MB PST. It converts in about 11 seconds and passes all three checks.

Run the tests with `python3 -m unittest discover -s tests`. The GitHub workflow
`.github/workflows/olm2pst-windows.yml` runs the same tests on Windows. It then
builds the two exes with PyInstaller and checks the exe's output with the verifier.

### Known limitations

- **Fixed in 0.1.1: PSTs from 0.1.0 can't be imported.** Version 0.1.0 wrote
  the allocation-map bits in the wrong order. SCANPST reports this as "Block is
  not allocated in AMAP", and Outlook can then overwrite data in the file.
  Convert again from the `.olm` rather than repairing an old PST. The bit
  order is now checked against Outlook-created PSTs and by a regression test.
- 0.1.2 writes the system tables Outlook adds when it first opens a PST
  (search template, receive-folder, outgoing-queue and three replication
  tables, plus an index node). It also uses Outlook's default "compressible"
  encoding. New Outlook's import opens the file read-only and couldn't open
  PSTs without these tables. Classic Outlook has opened a converted file and
  added its own search and index tables to it. A full import of a large archive with the fixed version is
  still to be confirmed, so test on a copy first. If Outlook reports a
  problem, send the `SCANPST.EXE` report; it pinpoints the cause.
- The exact layout of FMap/FPMap pages in files over 32 MB / 2 GB isn't fully
  described in [MS-PST]. Outlook 2007 SP2 and later don't use these pages.
- A single message larger than the `--split` size goes into its own file,
  which can be larger than the limit.
- libpst can't read messages with more than about 338 attachments. Those need
  a second-level subnode block (SIBLOCK), which libpst doesn't support; libpff
  and the spec do. Also run `readpst` with `-j 0`, because its parallel mode
  can misread any PST.
- Outlook's search index catches up on its own after the PST is opened. The
  converter doesn't pre-populate search queues.

[MS-PST]: https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-pst/
