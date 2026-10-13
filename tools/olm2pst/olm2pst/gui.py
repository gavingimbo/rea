"""Desktop window: preview an OLM archive in three panes, then convert it.

Uses Tkinter from the Python standard library. Work runs on background
threads; results come back to the Tk thread through a queue.
"""

from __future__ import annotations

import os
import queue
import threading
from datetime import datetime
from typing import Callable


def _local(when: datetime, fmt: str) -> str:
    """Format in local time; Windows cannot convert dates before 1970."""
    try:
        return when.astimezone().strftime(fmt)
    except (OSError, OverflowError, ValueError):
        return when.strftime(fmt)


def run(initial_source: str | None = None) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from .cli import default_output, parse_date, parse_size
    from .convert import FORMATS, Options, convert, folder_key
    from .model import MailMessage
    from .olm import OlmReader
    from .pst.messaging import html_to_text

    root = tk.Tk()
    root.title("OLM Converter")
    root.geometry("1100x700")
    root.minsize(820, 520)
    events: queue.Queue = queue.Queue()
    state: dict = {"reader": None, "folders": {}, "single": True, "busy": False}
    reader_lock = threading.Lock()

    def background(work: Callable[[], object], done: Callable[[object], None]) -> None:
        def target() -> None:
            try:
                events.put((done, work()))
            except Exception as exc:  # reported in the window instead of crashing it
                events.put((show_error, exc))
        threading.Thread(target=target, daemon=True).start()

    def pump() -> None:
        try:
            while True:
                callback, value = events.get_nowait()
                callback(value)
        except queue.Empty:
            pass
        root.after(100, pump)

    # -- top bar --------------------------------------------------------------
    top = ttk.Frame(root, padding=(10, 10, 10, 4))
    top.pack(fill="x")
    source = tk.StringVar(value=initial_source or "")
    ttk.Label(top, text="OLM file").pack(side="left")
    ttk.Entry(top, textvariable=source).pack(side="left", fill="x", expand=True, padx=6)
    ttk.Button(top, text="Browse…", command=lambda: pick_source()).pack(side="left")
    open_button = ttk.Button(top, text="Open", command=lambda: open_archive())
    open_button.pack(side="left", padx=(6, 0))

    # -- three panes ----------------------------------------------------------
    panes = ttk.PanedWindow(root, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=10, pady=4)
    folder_tree = ttk.Treeview(panes, columns=("count",), selectmode="extended")
    folder_tree.heading("#0", text="Folders")
    folder_tree.heading("count", text="Items")
    folder_tree.column("#0", width=220)
    folder_tree.column("count", width=60, anchor="e")
    panes.add(folder_tree, weight=1)

    right = ttk.PanedWindow(panes, orient="vertical")
    panes.add(right, weight=3)
    columns = ("date", "from", "subject", "att")
    message_list = ttk.Treeview(right, columns=columns, show="headings", selectmode="browse")
    for col, title, width in (("date", "Received", 140), ("from", "From", 180),
                              ("subject", "Subject", 420), ("att", "📎", 30)):
        message_list.heading(col, text=title)
        message_list.column(col, width=width, anchor="w" if col != "att" else "center")
    right.add(message_list, weight=2)
    preview = tk.Text(right, wrap="word", height=12, state="disabled", padx=8, pady=6)
    right.add(preview, weight=3)

    # -- options and convert ----------------------------------------------------
    bottom = ttk.Frame(root, padding=(10, 4, 10, 10))
    bottom.pack(fill="x")
    fmt = tk.StringVar(value="pst")
    output = tk.StringVar()
    only_selected = tk.BooleanVar(value=False)
    since = tk.StringVar()
    until = tk.StringVar()
    split = tk.StringVar()
    status = tk.StringVar(value="Choose an Outlook for Mac archive (.olm) and press Open.")

    row1 = ttk.Frame(bottom)
    row1.pack(fill="x")
    ttk.Label(row1, text="Save as").pack(side="left")
    ttk.Combobox(row1, textvariable=fmt, values=FORMATS, state="readonly", width=6).pack(side="left", padx=6)
    ttk.Entry(row1, textvariable=output).pack(side="left", fill="x", expand=True)
    ttk.Button(row1, text="Browse…", command=lambda: pick_output()).pack(side="left", padx=6)

    row2 = ttk.Frame(bottom)
    row2.pack(fill="x", pady=(6, 0))
    ttk.Checkbutton(row2, text="Only selected folders", variable=only_selected).pack(side="left")
    ttk.Label(row2, text="   Received from").pack(side="left")
    ttk.Entry(row2, textvariable=since, width=11).pack(side="left", padx=4)
    ttk.Label(row2, text="to (before)").pack(side="left")
    ttk.Entry(row2, textvariable=until, width=11).pack(side="left", padx=4)
    ttk.Label(row2, text="(YYYY-MM-DD)   Split PST at").pack(side="left")
    ttk.Entry(row2, textvariable=split, width=8).pack(side="left", padx=4)
    ttk.Label(row2, text="(e.g. 10GB)").pack(side="left")
    convert_button = ttk.Button(row2, text="Convert", command=lambda: start_convert())
    convert_button.pack(side="right")

    bar = ttk.Progressbar(bottom, mode="determinate")
    bar.pack(fill="x", pady=(8, 2))
    ttk.Label(bottom, textvariable=status, wraplength=1000, justify="left").pack(fill="x")

    # -- behaviour --------------------------------------------------------------
    def show_error(exc: object) -> None:
        state["busy"] = False
        convert_button.configure(state="normal")
        open_button.configure(state="normal")
        status.set(f"Error: {exc}")
        messagebox.showerror("OLM Converter", str(exc))

    def set_preview(text: str) -> None:
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("1.0", text)
        preview.configure(state="disabled")

    def pick_source() -> None:
        path = filedialog.askopenfilename(filetypes=[("Outlook for Mac archive", "*.olm"), ("All files", "*")])
        if path:
            source.set(path)
            open_archive()

    def pick_output() -> None:
        if fmt.get() == "pst":
            path = filedialog.asksaveasfilename(defaultextension=".pst", filetypes=[("Outlook data file", "*.pst")])
        else:
            path = filedialog.askdirectory()
        if path:
            output.set(path)

    def on_format(*_: object) -> None:
        if source.get():
            output.set(default_output(source.get(), fmt.get()))

    fmt.trace_add("write", on_format)

    def open_archive() -> None:
        path = source.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("OLM Converter", "Choose an existing .olm file.")
            return
        if state["reader"] is not None:
            state["reader"].close()
        open_button.configure(state="disabled")
        status.set("Reading archive…")
        output.set(default_output(path, fmt.get()))

        def work() -> tuple:
            reader = OlmReader(path)
            single = len(reader.accounts()) <= 1
            folders: dict[tuple[str, ...], list] = {}
            for item in reader.items(load_content=False):
                m = item.message
                when = m.received or m.sent
                folders.setdefault(folder_key(item.folder, single), []).append(
                    (item.position, when, m.sender.display() if m.sender else "", m.subject,
                     len(m.attachments), m.is_read))
            return reader, single, folders

        background(work, show_archive)

    def show_archive(result: tuple) -> None:
        reader, single, folders = result
        state.update(reader=reader, single=single, folders=folders)
        open_button.configure(state="normal")
        folder_tree.delete(*folder_tree.get_children())
        nodes: dict[tuple[str, ...], str] = {}
        for key in sorted(folders):
            for depth in range(1, len(key) + 1):
                part = key[:depth]
                if part not in nodes:
                    parent = nodes.get(part[:-1], "")
                    nodes[part] = folder_tree.insert(parent, "end", text=part[-1], open=depth < 2,
                                                     values=(len(folders.get(part, [])),))
        for node_key, node in nodes.items():
            folder_tree.item(node, tags=("/".join(node_key),))
        total = sum(len(v) for v in folders.values())
        skipped = ", ".join(f"{n} {k}" for k, n in reader.summary.skipped.items())
        status.set(f"{total} messages in {len(folders)} folders."
                   + (f" Not converted (mail only): {skipped}." if skipped else ""))
        message_list.delete(*message_list.get_children())
        set_preview("")

    def key_of(node: str) -> tuple[str, ...]:
        parts = []
        while node:
            parts.append(folder_tree.item(node, "text"))
            node = folder_tree.parent(node)
        return tuple(reversed(parts))

    def on_folder(_: object) -> None:
        sel = folder_tree.selection()
        message_list.delete(*message_list.get_children())
        if not sel:
            return
        rows = state["folders"].get(key_of(sel[0]), [])
        for i, (pos, when, sender, subject, natt, read) in enumerate(rows):
            stamp = _local(when, "%Y-%m-%d %H:%M") if when else ""
            message_list.insert("", "end", iid=str(i), values=(stamp, sender, subject or "(no subject)",
                                                               "📎" if natt else ""),
                                tags=() if read else ("unread",))
        message_list.tag_configure("unread", font=("TkDefaultFont", 10, "bold"))
        state["current"] = rows

    def on_message(_: object) -> None:
        sel = message_list.selection()
        if not sel:
            return
        position = state["current"][int(sel[0])][0]
        set_preview("Loading…")

        def work() -> MailMessage:
            with reader_lock:
                return state["reader"].load(position)

        background(work, show_message)

    def show_message(msg: MailMessage) -> None:
        def people(addresses: list) -> str:
            return "; ".join(f"{a.name} <{a.email}>" if a.name else a.email for a in addresses)

        lines = [f"From: {people([msg.sender]) if msg.sender else ''}", f"To: {people(msg.to)}"]
        if msg.cc:
            lines.append(f"Cc: {people(msg.cc)}")
        when = msg.received or msg.sent
        lines.append(f"Date: {_local(when, '%Y-%m-%d %H:%M %Z')}" if when else "Date: (none)")
        lines.append(f"Subject: {msg.subject}")
        if msg.attachments:
            lines.append("Attachments: " + ", ".join(f"{a.filename} ({len(a.data):,} bytes)"
                                                     for a in msg.attachments))
        body = msg.body_text or (html_to_text(msg.body_html) if msg.body_html else "")
        set_preview("\n".join(lines) + "\n" + "─" * 60 + "\n" + body)

    folder_tree.bind("<<TreeviewSelect>>", on_folder)
    message_list.bind("<<TreeviewSelect>>", on_message)

    def start_convert() -> None:
        if state["busy"]:
            return
        src, dst = source.get().strip(), output.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showerror("OLM Converter", "Choose an existing .olm file.")
            return
        if not dst:
            messagebox.showerror("OLM Converter", "Choose where to save the result.")
            return
        try:
            options = Options(
                since=parse_date(since.get().strip()) if since.get().strip() else None,
                until=parse_date(until.get().strip()) if until.get().strip() else None,
                split_bytes=parse_size(split.get().strip()) if split.get().strip() and fmt.get() == "pst" else None,
                overwrite=True,
            )
        except Exception as exc:
            messagebox.showerror("OLM Converter", str(exc))
            return
        if only_selected.get():
            options.include = ["/".join(key_of(n)) for n in folder_tree.selection()]
            if not options.include:
                messagebox.showerror("OLM Converter", "Select one or more folders first.")
                return
        if os.path.exists(dst) and not messagebox.askyesno("OLM Converter", f"{dst} exists. Replace it?"):
            return
        state["busy"] = True
        convert_button.configure(state="disabled")
        status.set("Converting…")

        def progress(done: int, total: int, folder: str) -> None:
            events.put((lambda v: (bar.configure(maximum=max(v[1], 1), value=v[0]),
                                   status.set(f"Converting {v[2]} ({v[0]}/{v[1]} files)…")),
                        (done, total, folder)))

        background(lambda: convert(src, dst, fmt.get(), options, progress=progress), finished)

    def finished(report: object) -> None:
        state["busy"] = False
        convert_button.configure(state="normal")
        bar.configure(value=bar.cget("maximum"))
        lines = report.lines()  # type: ignore[attr-defined]
        status.set("\n".join(lines[:4]))
        messagebox.showinfo("Finished", "\n".join(lines[:25]))

    if initial_source:
        root.after(200, open_archive)
    root.after(100, pump)
    root.mainloop()
    if state["reader"] is not None:
        state["reader"].close()
