from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_BAD_NAMES = {"auth.json", ".credentials.json", ".env", "site.toml"}
_CONTENT = [
    (re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----"), "PEM private-key header"),
    (re.compile(r'"private_key"\s*:'), 'JSON "private_key"'),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"xox[baprs]-"), "Slack token"),
]


def scan_paths(paths: list[Path], root: Path) -> list[str]:
    failures: list[str] = []
    for path in paths:
        relative = path.relative_to(root)
        parts = relative.parts
        if "internal" in parts or ".gsd" in parts:
            failures.append(f"{relative}:1: forbidden tracked path")
            continue
        name = path.name
        if name in _BAD_NAMES or name.startswith(".env.") or path.suffix.lower() in {".pem", ".p12", ".key"}:
            if not (name == "site.example.toml"):
                failures.append(f"{relative}:1: forbidden file name")
                continue
        try:
            data = path.read_bytes()
            if b"\x00" in data:
                continue
            data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
            for pattern, reason in _CONTENT:
                if pattern.search(line):
                    failures.append(f"{relative}:{number}: {reason}")
    return failures


def _tracked(root: Path) -> list[Path]:
    try:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
        return [root / item for item in result.stdout.decode().split("\0") if item]
    except (OSError, subprocess.SubprocessError):
        excluded = {".git", "internal", ".gsd", ".venv"}
        return [path for path in root.rglob("*") if path.is_file() and not excluded.intersection(path.relative_to(root).parts)]


if __name__ == "__main__":
    repo = Path(__file__).resolve().parent.parent
    problems = scan_paths(_tracked(repo), repo)
    for problem in problems:
        print(problem)
    sys.exit(1 if problems else 0)
