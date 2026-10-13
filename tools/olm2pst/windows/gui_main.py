"""Entry point of OLM2PST.exe: the desktop window (an .olm dropped on the exe opens directly)."""

import sys

from olm2pst.cli import main

raise SystemExit(main(["--gui", *sys.argv[1:2]]))
