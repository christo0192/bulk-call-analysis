from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_BAD_NAMES = {"auth.json", ".credentials.json", ".env", "site.toml"}
_PGP_BLOCK = "PGP " + "PRIVATE KEY BLOCK"
_CONTENT = [
    (re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----"), "PEM private-key header"),
    (re.compile(r'"private_key"\s*:'), 'JSON "private_key"'),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), "GitHub fine-grained token"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "API key"),
    (re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (re.compile(re.escape(_PGP_BLOCK)), "PGP private-key block"),
    (re.compile(r"xox[baprs]-"), "Slack token"),
]
_BINARY_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".doc",
    ".docx",
    ".gif",
    ".ico",
    ".jpg",
    ".jpeg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".wav",
    ".webp",
    ".zip",
}


def _decode_text(path: Path, data: bytes) -> str | None:
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or (
        len(data) >= 4
        and (data[1] == 0 or data[3] == 0)
        and sum(byte == 0 for byte in data) >= len(data) // 4
    ):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if not data:
        return ""
    nul_ratio = data.count(0) / len(data)
    nontext_ratio = sum(byte < 9 or 13 < byte < 32 for byte in data if byte != 0) / len(data)
    if nul_ratio > 0.1 or nontext_ratio > 0.3:
        return None
    return data.decode("utf-8", errors="replace")


def scan_paths(paths: list[Path], root: Path) -> list[str]:
    failures: list[str] = []
    for path in paths:
        try:
            relative = path.relative_to(root)
            data = path.read_bytes()
        except (OSError, ValueError) as exc:
            failures.append(f"{path}:1: unreadable file: {exc}")
            continue
        parts = relative.parts
        if parts and parts[0] in {"internal", ".gsd"}:
            failures.append(f"{relative}:1: forbidden tracked path")
            continue
        name = path.name.lower()
        if (
            name in _BAD_NAMES or name.startswith(".env.") or path.suffix.lower() in {".pem", ".p12", ".key"}
        ) and name != "site.example.toml":
            failures.append(f"{relative}:1: forbidden file name")
            continue
        if name.endswith(".log") or ".log." in name:
            failures.append(f"{relative}:1: tracked log file")
            continue
        text = _decode_text(path, data)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, reason in _CONTENT:
                if pattern.search(line):
                    failures.append(f"{relative}:{number}: {reason}")
    return failures


def _tracked(root: Path) -> list[Path]:
    try:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
        return [
            root / item for item in result.stdout.decode().split("\0") if item and (root / item).is_file()
        ]
    except (OSError, subprocess.SubprocessError):
        return [
            path
            for path in root.rglob("*")
            if path.is_file() and path.relative_to(root).parts[0] not in {".git", ".venv"}
        ]


if __name__ == "__main__":
    repo = Path(__file__).resolve().parent.parent
    problems = scan_paths(_tracked(repo), repo)
    for problem in problems:
        print(problem)
    sys.exit(1 if problems else 0)
