"""End-to-end tests: OLM fixture -> converter -> independent readers.

Run from tools/olm2pst:  python3 -m unittest discover -s tests
libpff (pip install libpff-python) and libpst (readpst) are used when present.
"""

from __future__ import annotations

import email
import glob
import hashlib
import mailbox
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fixture  # noqa: E402
from verify_pst import Checker  # noqa: E402

from olm2pst.convert import Options, convert, scan  # noqa: E402

try:
    import pypff  # type: ignore
except ImportError:  # optional
    pypff = None

READPST = shutil.which("readpst")
BIG = bytes(range(256)) * 400


class ConvertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.olm = os.path.join(self.tmp, "sample.olm")
        fixture.sample(self.olm)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def pst(self, options: Options | None = None, name: str = "out.pst") -> str:
        path = os.path.join(self.tmp, name)
        report = convert(self.olm, path, "pst", options)
        self.assertEqual(report.problems, [])
        for out in report.outputs:
            self.assertEqual(Checker(out).run(), [], out)
        return path

    def test_scan_lists_folders(self) -> None:
        stats = scan(self.olm)
        self.assertEqual(sorted(stats), [("Deleted Items",), ("Inbox",), ("Inbox", "Projects"), ("Sent Items",)])
        self.assertEqual(stats[("Inbox",)].messages, 4)
        self.assertEqual(stats[("Inbox",)].unread, 1)

    @unittest.skipIf(pypff is None, "libpff-python not installed")
    def test_pst_content_with_libpff(self) -> None:
        f = pypff.file()
        f.open(self.pst())
        top = f.root_folder.get_sub_folder(0)
        self.assertEqual(top.name, "Top of Personal Folders")
        folders = {top.get_sub_folder(i).name: top.get_sub_folder(i) for i in range(top.number_of_sub_folders)}
        self.assertEqual(sorted(folders), ["Deleted Items", "Inbox", "Sent Items"])
        inbox = folders["Inbox"]
        subjects = [inbox.get_sub_message(i).subject for i in range(inbox.number_of_sub_messages)]
        self.assertIn("Quarterly report", subjects)
        big = inbox.get_sub_message(subjects.index("RE: Unread with big attachment"))
        att = big.get_attachment(0)
        self.assertEqual(hashlib.md5(att.read_buffer(att.size)).digest(), hashlib.md5(BIG).digest())
        first = inbox.get_sub_message(subjects.index("Quarterly report"))
        self.assertEqual(first.sender_name, "Alice Example")
        self.assertIn("é".encode(), first.html_body)
        self.assertEqual(first.delivery_time, datetime(2024, 3, 1, 9, 15, 5))
        f.close()

    @unittest.skipIf(READPST is None, "readpst not installed")
    def test_pst_content_with_libpst(self) -> None:
        out = os.path.join(self.tmp, "readpst")
        os.mkdir(out)
        # -j 0: readpst's parallel mode shares one file offset between processes.
        subprocess.run([READPST, "-j", "0", "-q", "-D", "-M", "-o", out, self.pst()], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        files = [p for p in glob.glob(os.path.join(out, "**", "*"), recursive=True) if os.path.isfile(p)]
        self.assertEqual(len(files), 7)
        msgs = []
        for p in files:
            with open(p, "rb") as fh:
                msgs.append(email.message_from_binary_file(fh))
        by_subject = {m["Subject"]: m for m in msgs}
        unread = by_subject["RE: Unread with big attachment"]
        self.assertIsNone(unread["Status"])  # libpst writes "Status: RO" only for read mail
        self.assertEqual(by_subject["Quarterly report"]["Status"], "RO")
        payloads = [p.get_payload(decode=True) for p in unread.walk() if p.get_filename() == "big.bin"]
        self.assertEqual(payloads, [BIG])
        self.assertEqual(by_subject["Quarterly report"]["Cc"], "Bob <bob@example.com>")

    def test_amap_bit_order_matches_outlook(self) -> None:
        # Outlook numbers AMap slots from the most significant bit. SCANPST
        # rejected PSTs written with the opposite order ("Block is not
        # allocated in AMAP"), and Outlook then reused slots that held data.
        with open(self.pst(), "rb") as f:
            f.seek(0x4400)
            amap = f.read(496)
        # Slots 0-15 hold the AMap and PMap pages; the first data block starts
        # at slot 16, i.e. the most significant bit of byte 2.
        self.assertEqual(amap[:2], b"\xff\xff")
        self.assertTrue(amap[2] & 0x80)

    def test_folder_filters(self) -> None:
        path = os.path.join(self.tmp, "f.pst")
        report = convert(self.olm, path, "pst", Options(include=["Inbox"], exclude=["inbox/projects"]))
        self.assertEqual(report.messages, 4)
        self.assertEqual(report.folders, {("Inbox",)})
        self.assertEqual(Checker(path).run(), [])

    def test_date_filter(self) -> None:
        cut = datetime(2024, 3, 1, 9, 15, 5, tzinfo=timezone.utc)
        after = convert(self.olm, os.path.join(self.tmp, "a"), "eml", Options(since=cut))
        before = convert(self.olm, os.path.join(self.tmp, "b"), "eml", Options(until=cut))
        self.assertEqual((after.messages, before.messages), (7, 0))
        self.assertEqual(before.filtered_out, 7)

    def test_eml_and_mbox(self) -> None:
        eml_dir = os.path.join(self.tmp, "eml")
        convert(self.olm, eml_dir, "eml")
        emls = glob.glob(os.path.join(eml_dir, "**", "*.eml"), recursive=True)
        self.assertEqual(len(emls), 7)
        mbox_dir = os.path.join(self.tmp, "mbox")
        convert(self.olm, mbox_dir, "mbox")
        inbox = mailbox.mbox(os.path.join(mbox_dir, "Inbox.mbox"))
        self.assertEqual(len(inbox), 4)
        inline = [m for m in inbox if m["Subject"] == "Inline image"][0]
        related = [p for p in inline.walk() if p.get("Content-ID") == "<logo1>"]
        inbox.close()
        self.assertEqual(len(related), 1)

    def test_refuses_to_overwrite(self) -> None:
        path = self.pst()
        with self.assertRaises(FileExistsError):
            convert(self.olm, path, "pst")

    def test_split(self) -> None:
        many = os.path.join(self.tmp, "many.olm")
        body = "lorem ipsum " * 1200  # ~14 KB of text per message
        fixture.write_olm(many, {"Inbox": [fixture.email_xml(f"m{i}", text=body, message_id=f"<{i}@x>")
                                           for i in range(2500)]})
        out = os.path.join(self.tmp, "split.pst")
        report = convert(many, out, "pst", Options(split_bytes=16 * 1024 * 1024))
        self.assertGreater(len(report.outputs), 1)
        self.assertEqual(report.outputs[0], out)
        for part in report.outputs:
            self.assertLessEqual(os.path.getsize(part), 16 * 1024 * 1024)
            self.assertEqual(Checker(part).run(), [], part)
        self.assertEqual(report.messages, 2500)


if __name__ == "__main__":
    unittest.main()
