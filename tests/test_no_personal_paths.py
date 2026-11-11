"""AC-11.3 / catalog rule 17: nothing a user's machine-specific path may ship in the public repo."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Built from pieces so this file does not match itself.
PERSONAL_PATH = re.compile(r"(/mnt/c/Us" r"ers/|[A-Za-z]:[\/]Us" r"ers[\/]|/home/[a-z][\w.-]*/|/Us" r"ers/[a-z][\w.-]*/)", re.I)


def test_AC_11_3_no_personal_filesystem_path_in_tracked_files():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in files:
        if rel == "tests/test_no_personal_paths.py":
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if PERSONAL_PATH.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:90]}")
    assert not hits, "personal paths in tracked files:\n" + "\n".join(hits)
