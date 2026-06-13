"""Reject private paths, identities, and credentials in a public demo cast."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


ALLOWED_PROJECTS = {"webapp", "api-server", "data-pipeline", "dotfiles"}

_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_RULES = (
    ("Windows user path", re.compile(r"(?i)\b[a-z]:[\\/]+users[\\/]+")),
    ("WSL host-drive user path", re.compile(r"(?i)/mnt/[a-z]/users/")),
    ("non-demo Linux home", re.compile(r"/home/(?!demo(?:/|\b))[^/\s]+")),
    ("API key assignment", re.compile(
        r"(?i)\b(?:anthropic|openai|claude|aws)?_?api[_-]?key\s*[:=]\s*\S+"
    )),
    ("Anthropic API token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]+")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+")),
    ("private key header", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("localhost auth URL", re.compile(
        r"(?i)https?://(?:localhost|127\.0\.0\.1|\[::1\])"
        r"[^\s]*(?:token|key|auth|secret)=[^\s&]+"
    )),
    ("email address", re.compile(
        r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"
    )),
)
_DEMO_PROJECT_RE = re.compile(
    r"/home/demo/(?:work|saikai-demo/repos)/([A-Za-z0-9._-]+)"
)


def _plain(text: str) -> str:
    return _CSI_RE.sub("", _OSC_RE.sub("", text))


def audit_text(text: str, *, extra_deny: str | None = None) -> None:
    """Raise ValueError naming the matched rule, without echoing the match."""
    plain = _plain(text)
    for name, pattern in _RULES:
        if pattern.search(plain):
            raise ValueError(f"demo audit failed: {name}")

    for match in _DEMO_PROJECT_RE.finditer(plain):
        if match.group(1) not in ALLOWED_PROJECTS:
            raise ValueError("demo audit failed: unapproved fictional project")

    deny = os.environ.get("SAIKAI_DEMO_DENY", "") if extra_deny is None else extra_deny
    for index, raw_pattern in enumerate(deny.splitlines(), 1):
        pattern = raw_pattern.strip()
        if pattern and re.search(pattern, plain, flags=re.IGNORECASE):
            raise ValueError(f"demo audit failed: SAIKAI_DEMO_DENY rule {index}")


def audit_cast(path: Path) -> None:
    """Parse an asciinema v2 cast and audit its terminal output events."""
    output: list[str] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("demo audit failed: cast is unreadable") from exc
    if not lines:
        raise ValueError("demo audit failed: cast is empty")

    for line_number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"demo audit failed: invalid cast JSON at line {line_number}"
            ) from exc
        if isinstance(record, list) and len(record) >= 3 and record[1] == "o":
            output.append(str(record[2]))
    audit_text("".join(output))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cast", type=Path)
    args = parser.parse_args()
    try:
        audit_cast(args.cast)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"demo audit passed: {args.cast}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
