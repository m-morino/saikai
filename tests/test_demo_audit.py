"""Tests for the public-demo cast leak auditor."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_demo_cast import audit_cast, audit_text


def test_audit_accepts_fictional_demo_identity():
    audit_text("cwd: /home/demo/work/webapp\nClaude Code")
    audit_text("fixture: /home/demo/saikai-demo/repos/api-server")


def test_audit_rejects_private_paths_and_tokens():
    unsafe = (
        r"C:\Users\realname\repo",
        "C:/Users/realname/repo",
        "/mnt/c/Users/realname/repo",
        "/home/realname/private",
        "/home/demo/work/private-project",
        "ANTHROPIC_API_KEY=secret",
        "sk-ant-api03-secret",
        "Authorization: Bearer secret",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "http://localhost:8080/callback?token=secret",
        "real.person@example.com",
    )
    for text in unsafe:
        try:
            audit_text(text)
        except ValueError:
            pass
        else:
            raise AssertionError(text)


def test_audit_cast_reads_only_output_events():
    cast = Path(tempfile.mkdtemp()) / "demo.cast"
    lines = [
        json.dumps({"version": 2, "width": 128, "height": 35}),
        json.dumps([0.1, "o", "cwd: /home/demo/work/webapp\r\n"]),
        json.dumps([0.2, "i", "ANTHROPIC_API_KEY=typed-but-not-echoed"]),
        json.dumps([0.3, "o", "Claude Code\r\n"]),
    ]
    cast.write_text("\n".join(lines) + "\n", encoding="utf-8")
    audit_cast(cast)


if __name__ == "__main__":
    test_audit_accepts_fictional_demo_identity()
    print("PASS test_audit_accepts_fictional_demo_identity")
    test_audit_rejects_private_paths_and_tokens()
    print("PASS test_audit_rejects_private_paths_and_tokens")
    test_audit_cast_reads_only_output_events()
    print("PASS test_audit_cast_reads_only_output_events")
