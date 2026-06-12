"""Agent-provider contract regressions.

Run:  python tests/test_providers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saikai_provider import CodexProvider, ClaudeProvider, get_provider


def _env():
    return {"PATH": os.environ.get("PATH", ""), "KEEP": "yes"}


def test_claude_provider_contract():
    provider = ClaudeProvider()
    caps = provider.capabilities
    assert provider.id == "claude"
    assert provider.status_profile == "claude"
    assert caps.can_resume and caps.can_create and caps.can_preassign_id
    assert caps.has_reliable_live_status
    assert caps.has_transcript_changes and caps.has_desktop_sync

    resume = provider.build_resume(
        "sid-1", cwd="/work", env=_env(), extra_args=["--permission-mode", "auto"],
        executable="claude-test",
    )
    assert resume.argv == [
        "claude-test", "--resume", "sid-1", "--permission-mode", "auto",
    ]
    assert resume.cwd == "/work" and resume.session_id == "sid-1"
    assert resume.env["KEEP"] == "yes"

    new = provider.build_new(
        cwd="/work", requested_id="sid-new", env=_env(), executable="claude-test",
    )
    assert new.argv == ["claude-test", "--session-id", "sid-new"]
    assert new.session_id == "sid-new"


def test_codex_provider_contract():
    provider = CodexProvider()
    caps = provider.capabilities
    assert provider.id == "codex"
    assert provider.status_profile == "generic"
    assert caps.can_resume and caps.can_create
    assert not caps.can_preassign_id
    assert not caps.has_reliable_live_status
    assert not caps.has_transcript_changes and not caps.has_desktop_sync

    resume = provider.build_resume(
        "thread-1", cwd="/work", env=_env(), executable="codex-test",
    )
    assert resume.argv == ["codex-test", "resume", "thread-1"]
    assert resume.cwd == "/work" and resume.session_id == "thread-1"

    new = provider.build_new(
        cwd="/work", requested_id="ignored-id", env=_env(), executable="codex-test",
    )
    assert new.argv == ["codex-test"]
    assert new.session_id is None


def test_provider_registry_is_explicit():
    assert isinstance(get_provider("claude"), ClaudeProvider)
    assert isinstance(get_provider("codex"), CodexProvider)
    try:
        get_provider("unknown")
    except ValueError as exc:
        assert "unknown provider" in str(exc)
    else:
        raise AssertionError("unknown provider must fail")


if __name__ == "__main__":
    test_claude_provider_contract()
    print("PASS test_claude_provider_contract")
    test_codex_provider_contract()
    print("PASS test_codex_provider_contract")
    test_provider_registry_is_explicit()
    print("PASS test_provider_registry_is_explicit")
    print("ALL PASS")
