"""Headless tests for saikai memory-bound fixes (resource code-review).

Run:  python tests/test_resource_bounds.py
"""
import os
import sys
import tempfile
from pathlib import Path

# Point saikai at a throwaway home BEFORE importing it (it derives CACHE_DIR /
# state files from Path.home() at import time). Mirrors the pattern in
# tests/test_keyboard_leader.py:18-25.
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-res-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def test_na_cache_is_bounded():
    """_needs_attention's cache must not grow without bound as distinct session
    ids accumulate over a long-lived picker (resource #8)."""
    cache = {}
    for i in range(5000):
        saikai._needs_attention({"id": f"s{i}", "mtime": 0}, cache)  # no jsonl_path -> False
    assert len(cache) <= 4097, f"cache grew unbounded: {len(cache)}"


def test_load_severity_bands():
    """warn is the precursor band (within 15 points of the gate); crit at/over."""
    assert saikai._load_severity(None, 85) == "ok"
    assert saikai._load_severity(50, 85) == "ok"
    assert saikai._load_severity(69.9, 85) == "ok"
    assert saikai._load_severity(70, 85) == "warn"     # 85 - 15
    assert saikai._load_severity(84, 85) == "warn"
    assert saikai._load_severity(85, 85) == "crit"
    assert saikai._load_severity(97, 95) == "crit"     # posix default gate 95


class _MS:
    """Minimal _MemStatus stand-in for the pure segment formatter."""
    def __init__(self, load, avail_mb):
        self.load = load
        self.avail_phys_mb = avail_mb


def test_live_ram_segment_estimate_and_severity_colour():
    # No memory status -> bare count, no RAM claims.
    assert saikai._live_ram_segment(3, "", None, 2, 600, 85) == "Live: 3"
    # Healthy: green load, green fit, saikai's estimated share shown (8*600/1024).
    s = saikai._live_ram_segment(8, "", _MS(60, 4096), 3, 600, 85)
    assert "Live: 8~4.7G" in s, s
    assert "[green]60% RAM[/green]" in s, s
    assert "[green]~3 fit[/green]" in s and "4.0G free" in s, s
    assert "⚠" not in s, "no warning sign while healthy"
    # Precursor (warn band): yellow + warning sign, BEFORE the gate trips.
    s2 = saikai._live_ram_segment(8, "", _MS(75, 2048), 1, 600, 85)
    assert "[yellow]" in s2 and "⚠" in s2 and "75% RAM" in s2, s2
    # Crit: red load + red ~0 fit.
    s3 = saikai._live_ram_segment(8, "", _MS(90, 512), 0, 600, 85)
    assert "[red]" in s3 and "90% RAM" in s3 and "[red]~0 fit[/red]" in s3, s3


def test_ctx_tokens_reads_last_usage_block(tmp_path=None):
    import json, tempfile, os
    d = tempfile.mkdtemp(prefix="saikai-ctx-")
    p = os.path.join(d, "s.jsonl")
    recs = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000,
                      "cache_creation_input_tokens": 200, "output_tokens": 50}}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 131, "cache_read_input_tokens": 715734,
                      "cache_creation_input_tokens": 4017, "output_tokens": 4229}}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    # last usage block: 131 + 715734 + 4017
    assert saikai._ctx_tokens_from_jsonl(p) == 719882
    # no usage anywhere -> None
    p2 = os.path.join(d, "n.jsonl")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    assert saikai._ctx_tokens_from_jsonl(p2) is None
    # missing file -> None (never raises)
    assert saikai._ctx_tokens_from_jsonl(os.path.join(d, "nope.jsonl")) is None


def test_ctx_window_inferred_from_observed_tokens():
    # message.model lacks the [1m] suffix, so infer the tier from the count.
    assert saikai._ctx_window_for(96_000) == 200_000
    assert saikai._ctx_window_for(200_000) == 200_000
    assert saikai._ctx_window_for(719_882) == 1_000_000     # this repo's real session
    assert saikai._ctx_window_for(1_200_000) == 1_000_000   # clamp to top tier
    assert saikai._ctx_window_for(50_000, override=500_000) == 500_000


def test_lineage_sidecar_roundtrip():
    # _set_lineage(child, parent, parent_jsonl) persists; _load_lineage reads it back.
    saikai._set_lineage("child-sid", "parent-sid", "/path/parent.jsonl")
    lin = saikai._load_lineage()
    assert lin["child-sid"]["parent"] == "parent-sid"
    assert lin["child-sid"]["parent_jsonl"] == "/path/parent.jsonl"
    assert "ts" in lin["child-sid"]


def test_b2_step_sequence_orders_clear_after_confirm_and_idle():
    """b2 (Task 11) is a tick-driven state machine: the destructive /clear must
    come AFTER the user confirm AND after the handoff settles, and the reseed
    must reference the parent handoff/prompt. Pure: assert the ordered shape."""
    seq = list(saikai._b2_step_sequence())
    # the spec'd states, all present
    for st in ("inject_handoff", "await_handoff_idle", "extract_prompt",
               "confirm", "inject_clear", "detect_child", "inject_reseed",
               "record_lineage"):
        assert st in seq, f"missing state {st!r}: {seq}"
    i = {st: seq.index(st) for st in seq}
    # the load-bearing safety invariant: /clear is gated behind the confirm
    # AND behind the handoff having gone idle.
    assert i["inject_clear"] > i["confirm"], seq
    assert i["inject_clear"] > i["await_handoff_idle"], seq
    # handoff is injected, then we wait for it, then read the prompt out, then
    # the human confirms — only then do we clear.
    assert i["inject_handoff"] < i["await_handoff_idle"] < i["extract_prompt"] < i["confirm"], seq
    # detect the fresh child before reseeding it, and record lineage last.
    assert i["inject_clear"] < i["detect_child"] < i["inject_reseed"] < i["record_lineage"], seq


def test_extract_handoff_prompt_slices_new_session_block():
    """The reseed prompt is the fenced NEW SESSION PROMPT block in the last
    assistant turn (the /handoff output). Slice it out; tolerate prose around
    the fence and varied fence languages."""
    ex = saikai._extract_handoff_prompt
    # ``` fence with a NEW SESSION PROMPT header inside
    body = (
        "Here's the handoff.\n\n"
        "```\n"
        "NEW SESSION PROMPT\n"
        "You are picking up saikai Task 11. The parent did X and Y.\n"
        "Continue with Z.\n"
        "```\n"
        "Good luck!"
    )
    got = ex(body)
    assert got is not None
    assert "picking up saikai Task 11" in got
    assert "Continue with Z." in got
    # the surrounding prose and the fence markers are not part of the prompt
    assert "Here's the handoff" not in got
    assert "Good luck!" not in got
    assert "```" not in got
    # header-as-markdown variant (## NEW SESSION PROMPT) with no fence still works
    body2 = (
        "blah\n\n## NEW SESSION PROMPT\n\n"
        "Resume the build from the failing test.\n\nmore"
    )
    got2 = ex(body2)
    assert got2 is not None and "Resume the build from the failing test." in got2
    # no NEW SESSION PROMPT anywhere -> None (never guess)
    assert ex("just an ordinary assistant reply, no handoff here") is None
    assert ex("") is None


def test_last_assistant_text_from_jsonl_reads_tail():
    import json, tempfile, os
    d = tempfile.mkdtemp(prefix="saikai-b2-")
    p = os.path.join(d, "s.jsonl")
    recs = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "first answer"}]}},
        {"type": "user", "message": {"role": "user", "content": "/handoff"}},
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "```\nNEW SESSION PROMPT\nresume me\n```"}]}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    txt = saikai._last_assistant_text_from_jsonl(p)
    assert txt is not None and "NEW SESSION PROMPT" in txt and "resume me" in txt
    # and it composes with the extractor
    assert saikai._extract_handoff_prompt(txt) == "resume me"
    # missing file -> None, never raises
    assert saikai._last_assistant_text_from_jsonl(os.path.join(d, "nope.jsonl")) is None


def test_first_cwd_from_jsonl_scans_early_records():
    """Spike finding #3: cwd is NOT on record 1 of a freshly /clear'd child
    (record 1 is {"type":"mode"}). The detector must scan the first several
    records for the first cwd, not just record 1."""
    import json, tempfile, os
    d = tempfile.mkdtemp(prefix="saikai-b2cwd-")
    p = os.path.join(d, "child.jsonl")
    recs = [
        {"type": "mode", "sessionId": "child-xyz"},          # rec 1: no cwd
        {"type": "file-history-snapshot"},                    # rec 2: no cwd
        {"type": "attachment", "cwd": "/home/alex/code/demo", # rec 3: first cwd
         "timestamp": "2026-06-17T10:00:05.000Z"},
        {"type": "user", "cwd": "/home/alex/code/demo",
         "message": {"content": "go"}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    assert saikai._first_cwd_from_jsonl(p) == "/home/alex/code/demo"
    # a transcript with no cwd at all -> None
    p2 = os.path.join(d, "nocwd.jsonl")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "mode", "sessionId": "x"}) + "\n")
    assert saikai._first_cwd_from_jsonl(p2) is None
    assert saikai._first_cwd_from_jsonl(os.path.join(d, "missing.jsonl")) is None


def test_bind_cleared_child_falsifiable_detection():
    """Spike finding #6: exactly 1 new file per /clear, but unrelated new
    *.jsonl appear from other lifecycle events. Bind the child as: the FIRST
    new sid whose first-record cwd matches the pane AND ts post-dates the clear;
    on 0 or >=2 candidates -> None (record NO lineage, never guess)."""
    import json, tempfile, os
    proj = tempfile.mkdtemp(prefix="saikai-b2bind-")
    pane_cwd = "/home/alex/code/demo"

    def _write(stem, cwd, ts):
        recs = [
            {"type": "mode", "sessionId": stem},
            {"type": "attachment", "cwd": cwd, "timestamp": ts},
        ]
        p = os.path.join(proj, f"{stem}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")
        return p

    parent = "parent-sid"
    _write(parent, pane_cwd, "2026-06-17T09:00:00.000Z")     # pre-existing
    pre = {parent}
    clear_ts = "2026-06-17T10:00:00.000Z"

    # Happy path: exactly one new sid, matching cwd, ts after the clear.
    child = "child-sid"
    _write(child, pane_cwd, "2026-06-17T10:00:03.000Z")
    got = saikai._bind_cleared_child(proj, pre, pane_cwd, clear_ts)
    assert got == child, got

    # Contamination: a sibling pane's new session in a DIFFERENT cwd also lands.
    # cwd filter rejects it -> still exactly one valid candidate.
    _write("sibling-sid", "/home/alex/code/other", "2026-06-17T10:00:04.000Z")
    assert saikai._bind_cleared_child(proj, pre, pane_cwd, clear_ts) == child

    # Ambiguous: a SECOND matching-cwd new sid post-dating the clear -> None.
    _write("child2-sid", pane_cwd, "2026-06-17T10:00:06.000Z")
    assert saikai._bind_cleared_child(proj, pre, pane_cwd, clear_ts) is None

    # Zero candidates (none post-date the clear) -> None.
    proj2 = tempfile.mkdtemp(prefix="saikai-b2bind0-")
    _write_old = os.path.join(proj2, "old.jsonl")
    with open(_write_old, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in [
            {"type": "mode", "sessionId": "old"},
            {"type": "attachment", "cwd": pane_cwd,
             "timestamp": "2026-06-17T08:00:00.000Z"}]) + "\n")
    assert saikai._bind_cleared_child(proj2, {"old"}, pane_cwd, clear_ts) is None


def test_bind_cleared_child_clear_ts_timezone_robust():
    """Regression: a child's transcript `timestamp` is UTC (trailing 'Z'); the
    recorded clear instant must compare correctly across host timezones. A naive
    LOCAL clear_ts on a +UTC-offset host (e.g. JST, UTC+9) string-sorts AFTER the
    child's earlier-looking UTC ts, which used to reject the only valid child and
    silently drop b2 lineage. A tz-aware compare must still bind it."""
    import json, tempfile, os
    from datetime import datetime, timezone, timedelta
    proj = tempfile.mkdtemp(prefix="saikai-b2tz-")
    pane_cwd = "/home/alex/code/demo"
    # the child claude mints ~now, written in the real transcript format (UTC 'Z').
    child_ts = (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    with open(os.path.join(proj, "child-sid.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in [
            {"type": "mode", "sessionId": "child-sid"},
            {"type": "attachment", "cwd": pane_cwd, "timestamp": child_ts},
        ]) + "\n")
    # A naive LOCAL clear_ts (what the machine used to record). On a +offset host
    # its string sorts after the child's UTC ts; a tz-aware compare interprets the
    # naive value as local time and still recognises the child as post-clear.
    clear_ts_naive_local = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    assert saikai._bind_cleared_child(proj, set(), pane_cwd, clear_ts_naive_local) \
        == "child-sid", f"child wrongly rejected (naive clear_ts={clear_ts_naive_local!r})"
    # the fixed generation path records UTC, directly comparable to the 'Z' ts.
    clear_ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert saikai._bind_cleared_child(proj, set(), pane_cwd, clear_ts_utc) == "child-sid"


def test_ctx_gauge_segment_formats_and_colours():
    # None tokens -> empty (no usage yet / unreadable).
    assert saikai._ctx_gauge_segment(None, 200_000) == ""
    # healthy: green, K-rounded, percent.
    s = saikai._ctx_gauge_segment(96_000, 200_000)
    assert "ctx 96K/200K (48%)" in s and "[green]" in s
    # 1M window, heavy: 719882/1.0M = 72% -> red (>= high band 70).
    s2 = saikai._ctx_gauge_segment(719_882, 1_000_000)
    assert "720K/1.0M (72%)" in s2 and "[red]" in s2
    # warn band (>= 55, < 70) -> yellow.
    s3 = saikai._ctx_gauge_segment(120_000, 200_000)   # 60%
    assert "[yellow]" in s3


if __name__ == "__main__":
    test_na_cache_is_bounded()
    print("PASS test_na_cache_is_bounded")
    test_load_severity_bands()
    print("PASS test_load_severity_bands")
    test_live_ram_segment_estimate_and_severity_colour()
    print("PASS test_live_ram_segment_estimate_and_severity_colour")
    test_ctx_tokens_reads_last_usage_block()
    print("PASS test_ctx_tokens_reads_last_usage_block")
    test_ctx_window_inferred_from_observed_tokens()
    print("PASS test_ctx_window_inferred_from_observed_tokens")
    test_ctx_gauge_segment_formats_and_colours()
    print("PASS test_ctx_gauge_segment_formats_and_colours")
    test_lineage_sidecar_roundtrip()
    print("PASS test_lineage_sidecar_roundtrip")
    test_b2_step_sequence_orders_clear_after_confirm_and_idle()
    print("PASS test_b2_step_sequence_orders_clear_after_confirm_and_idle")
    test_extract_handoff_prompt_slices_new_session_block()
    print("PASS test_extract_handoff_prompt_slices_new_session_block")
    test_last_assistant_text_from_jsonl_reads_tail()
    print("PASS test_last_assistant_text_from_jsonl_reads_tail")
    test_first_cwd_from_jsonl_scans_early_records()
    print("PASS test_first_cwd_from_jsonl_scans_early_records")
    test_bind_cleared_child_falsifiable_detection()
    print("PASS test_bind_cleared_child_falsifiable_detection")
    test_bind_cleared_child_clear_ts_timezone_robust()
    print("PASS test_bind_cleared_child_clear_ts_timezone_robust")
