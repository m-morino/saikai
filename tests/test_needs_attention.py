"""Headless tests for the 'Needs input' heuristic (_needs_attention).

A session whose transcript ends in Claude Code's '[Request interrupted by user]'
control marker was STOPPED by the user, not left waiting on them, so it must NOT
be flagged 'Needs input'. Genuine unanswered human prompts still must be.

Run:  python tests/test_needs_attention.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def _na(records):
    """Run _needs_attention against a temp transcript ending in `records`."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    try:
        return saikai._needs_attention(
            {"id": "t", "mtime": 1, "jsonl_path": path}, {})
    finally:
        os.unlink(path)


def _user_text(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def test_genuine_user_prompt_needs_attention():
    """A real human prompt left unanswered -> the assistant owes a reply."""
    assert _na([_assistant_text("done"),
                _user_text("please also add tests")]) is True


def test_interrupt_marker_is_not_needs_attention():
    """'[Request interrupted by user]' is a STOP signal, not a pending prompt."""
    assert _na([_assistant_text("working..."),
                _user_text("[Request interrupted by user]")]) is False


def test_interrupt_marker_tool_use_variant():
    assert _na([_assistant_text("working..."),
                _user_text("[Request interrupted by user for tool use]")]) is False


def test_interrupt_marker_as_plain_string_content():
    """Some transcript versions store content as a bare string."""
    rec = {"type": "user",
           "message": {"role": "user", "content": "[Request interrupted by user]"}}
    assert _na([_assistant_text("working..."), rec]) is False


def test_tool_result_turn_is_not_needs_attention():
    """type:user tool_result turns are auto-generated, not human prompts."""
    rec = {"type": "user", "message": {"role": "user",
           "content": [{"type": "tool_result", "content": "ok"}]}}
    assert _na([_assistant_text("calling tool"), rec]) is False


def test_assistant_last_is_not_needs_attention():
    assert _na([_user_text("hi"), _assistant_text("hello")]) is False


if __name__ == "__main__":
    test_genuine_user_prompt_needs_attention()
    test_interrupt_marker_is_not_needs_attention()
    test_interrupt_marker_tool_use_variant()
    test_interrupt_marker_as_plain_string_content()
    test_tool_result_turn_is_not_needs_attention()
    test_assistant_last_is_not_needs_attention()
    print("PASS test_needs_attention")
