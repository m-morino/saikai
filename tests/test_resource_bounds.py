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
    # _ctx_usage_from_jsonl also returns that turn's model id
    assert saikai._ctx_usage_from_jsonl(p) == (719882, "claude-opus-4-8")
    # no usage anywhere -> None
    p2 = os.path.join(d, "n.jsonl")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    assert saikai._ctx_tokens_from_jsonl(p2) is None
    assert saikai._ctx_usage_from_jsonl(p2) == (None, None)
    # missing file -> None (never raises)
    assert saikai._ctx_tokens_from_jsonl(os.path.join(d, "nope.jsonl")) is None
    assert saikai._ctx_usage_from_jsonl(os.path.join(d, "nope.jsonl")) == (None, None)


def test_ctx_window_inferred_from_observed_tokens():
    # message.model lacks the [1m] suffix, so infer the tier from the count.
    assert saikai._ctx_window_for(96_000) == 200_000
    assert saikai._ctx_window_for(200_000) == 200_000
    assert saikai._ctx_window_for(719_882) == 1_000_000     # this repo's real session
    assert saikai._ctx_window_for(1_200_000) == 1_000_000   # clamp to top tier
    assert saikai._ctx_window_for(50_000, override=500_000) == 500_000


def test_ctx_window_model_capacity():
    # A 1M-capable model (opus-4 / sonnet-4 families) defaults to the 1M window even
    # under 200K: a 1M session reading 150K is 15%, not the 75% the bare tier
    # inference shows. The base model id can't prove [1m] was on, but 1M is the
    # common mode now, so default to it (SAIKAI_CTX_WINDOW pins a 200K-mode session).
    assert saikai._model_supports_1m("claude-opus-4-8")
    assert saikai._model_supports_1m("claude-sonnet-4-6")
    assert not saikai._model_supports_1m("claude-haiku-4-5")
    assert not saikai._model_supports_1m(None)
    assert not saikai._model_supports_1m("")
    assert saikai._ctx_window_for(150_000, model="claude-opus-4-8") == 1_000_000
    assert saikai._ctx_window_for(150_000, model="claude-sonnet-4-6") == 1_000_000
    # non-1M / unknown / None model -> smallest-fitting tier (unchanged)
    assert saikai._ctx_window_for(150_000, model="claude-haiku-4-5") == 200_000
    assert saikai._ctx_window_for(150_000, model=None) == 200_000
    # override still wins over the model default
    assert saikai._ctx_window_for(150_000, model="claude-opus-4-8", override=200_000) == 200_000


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
               "verify_reseed", "record_lineage"):
        assert st in seq, f"missing state {st!r}: {seq}"
    i = {st: seq.index(st) for st in seq}
    # the load-bearing safety invariant: /clear is gated behind the confirm
    # AND behind the handoff having gone idle.
    assert i["inject_clear"] > i["confirm"], seq
    assert i["inject_clear"] > i["await_handoff_idle"], seq
    # handoff is injected, then we wait for it, then read the prompt out, then
    # the human confirms — only then do we clear.
    assert i["inject_handoff"] < i["await_handoff_idle"] < i["extract_prompt"] < i["confirm"], seq
    # detect the fresh child before reseeding it, VERIFY the reseed actually
    # submitted (the post-/clear re-init absorbs a too-early CR), lineage last.
    assert (i["inject_clear"] < i["detect_child"] < i["inject_reseed"]
            < i["verify_reseed"] < i["record_lineage"]), seq


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
    # header/bold marker FOLLOWED BY a fenced block that does not repeat the
    # marker inside — the shape models produce most often when they add a
    # heading. The audit found this returned None (silent checkpoint abort):
    # the bare-mode scan stopped AT the fence and extracted "". (#audit-b2-extract)
    body_hdr_fence = (
        "summary...\n\n## NEW SESSION PROMPT\n```\n"
        "Resume X from the failing test.\nDo Y next.\n```\n"
    )
    got_hf = ex(body_hdr_fence)
    assert got_hf is not None and "Resume X from the failing test." in got_hf, got_hf
    assert "```" not in (got_hf or ""), got_hf
    body_bold_fence = (
        "summary...\n\n**NEW SESSION PROMPT**\n```text\n"
        "Resume Z with the flag set.\n```\ntrailing prose"
    )
    got_bf = ex(body_bold_fence)
    assert got_bf is not None and "Resume Z with the flag set." in got_bf, got_bf
    assert "trailing prose" not in (got_bf or ""), got_bf
    # an assistant that ECHOES the marker in PROSE before the real fenced block
    # (e.g. the improved prompt tells it to "end with ... NEW SESSION PROMPT", so
    # the reply narrates that) must NOT make the extractor lock onto the prose —
    # it must prefer the marker that sits INSIDE a ``` fence.
    body3 = (
        "I'll summarize, then give the NEW SESSION PROMPT below.\n"
        "Recap of what we did:\n"
        "- explored the parser\n"
        "- fixed the bug\n\n"
        "```\n"
        "NEW SESSION PROMPT\n"
        "Resume: run the failing test, then ship.\n"
        "```\n"
    )
    got3 = ex(body3)
    assert got3 is not None and "Resume: run the failing test, then ship." in got3
    assert "Recap of what we did" not in got3, f"locked onto the prose echo: {got3!r}"
    # an EARLIER example/echo fenced block must NOT win over the real trailing one
    # — the prompt says "END with ONE fenced block", so the real block is LAST.
    body4 = (
        "Here's the format I'll use:\n"
        "```\n"
        "NEW SESSION PROMPT\n"
        "<your goal, paths, next step here>\n"
        "```\n"
        "Now the real one:\n"
        "```\n"
        "NEW SESSION PROMPT\n"
        "Resume the parser fix at saikai.py:3100; run the failing test.\n"
        "```\n"
    )
    got4 = ex(body4)
    assert got4 is not None and "Resume the parser fix" in got4
    assert "<your goal" not in got4, f"locked onto the example block: {got4!r}"
    # a ~~~ (tilde) CommonMark fence is valid: recognise its closer, don't swallow it
    body5 = "ok\n~~~\nNEW SESSION PROMPT\nResume X.\n~~~\n"
    got5 = ex(body5)
    assert got5 is not None and got5.strip() == "Resume X.", f"~~~ fence mishandled: {got5!r}"
    # no NEW SESSION PROMPT anywhere -> None (never guess)
    assert ex("just an ordinary assistant reply, no handoff here") is None
    assert ex("") is None


def test_resolve_handoff_prompt_override():
    """The b2 handoff prompt is overridable via SAIKAI_HANDOFF_PROMPT_FILE, but the
    `NEW SESSION PROMPT` contract is non-negotiable: a file that drops it is
    rejected (warn + fall back to the built-in), never silently used."""
    import os, tempfile
    os.environ.pop("SAIKAI_HANDOFF_PROMPT_FILE", None)
    # no override -> built-in default, no warning
    prompt, note = saikai._resolve_handoff_prompt()
    assert prompt == saikai._B2_HANDOFF_PROMPT and note is None
    d = tempfile.mkdtemp(prefix="saikai-hp-")
    try:
        # a valid override (keeps the contract marker) -> used, no warning
        good = os.path.join(d, "good.md")
        with open(good, "w", encoding="utf-8") as f:
            f.write("My custom handoff. End with a fenced NEW SESSION PROMPT block.")
        os.environ["SAIKAI_HANDOFF_PROMPT_FILE"] = good
        prompt, note = saikai._resolve_handoff_prompt()
        assert "My custom handoff." in prompt and note is None
        # an override that DROPPED the contract -> reject + warn + built-in default
        bad = os.path.join(d, "bad.md")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("Just summarise things. (no marker line here)")
        os.environ["SAIKAI_HANDOFF_PROMPT_FILE"] = bad
        prompt, note = saikai._resolve_handoff_prompt()
        assert prompt == saikai._B2_HANDOFF_PROMPT
        assert note and "NEW SESSION PROMPT" in note
    finally:
        os.environ.pop("SAIKAI_HANDOFF_PROMPT_FILE", None)


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


def test_first_ts_from_jsonl_scans_early_records():
    """The first ISO8601 `timestamp` drives the post-/clear ordering check; like
    cwd it is NOT on record 1 of a fresh child (record 1 is {"type":"mode"}). Scan
    the first several records, not just record 1. None when absent / unreadable."""
    import json, tempfile, os
    d = tempfile.mkdtemp(prefix="saikai-b2ts-")
    p = os.path.join(d, "child.jsonl")
    recs = [
        {"type": "mode", "sessionId": "child-xyz"},            # rec 1: no timestamp
        {"type": "file-history-snapshot"},                      # rec 2: no timestamp
        {"type": "attachment", "cwd": "/home/alex/code/demo",   # rec 3: first ts
         "timestamp": "2026-06-17T10:00:05.000Z"},
    ]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    assert saikai._first_ts_from_jsonl(p) == "2026-06-17T10:00:05.000Z"
    # no timestamp anywhere -> None
    p2 = os.path.join(d, "nots.jsonl")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "mode", "sessionId": "x"}) + "\n")
    assert saikai._first_ts_from_jsonl(p2) is None
    # missing file -> None (never raises)
    assert saikai._first_ts_from_jsonl(os.path.join(d, "missing.jsonl")) is None


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


def test_hostile_inputs_degrade_instead_of_raising():
    """Internal-layer hostile-input battery (#audit-hostile-*): every helper
    that renders or parses USER-derived / on-disk data must degrade to a calm
    default instead of raising — one corrupt record or hand-edited pref file
    must never break every list rebuild."""
    import json as _json
    import tempfile as _tf
    from pathlib import Path as _P
    import saikai_terminal as st

    # fmt_ts: None/int first_ts must not TypeError inside its except handler
    assert saikai.fmt_ts(None) == ""
    assert saikai.fmt_ts(12345) == ""
    assert saikai.fmt_ts("garbage-string")[:7] == "garbage"
    # _ctx_severity: unknown fill reads calm
    assert saikai._ctx_severity(None) == "ok"
    # usage coercion: corrupt/foreign usage fields degrade to 0, not ValueError
    assert saikai._usage_int("12k") == 0
    assert saikai._usage_int(None) == 0
    assert saikai._usage_int(7) == 7
    with _tf.TemporaryDirectory() as td:
        j = _P(td) / "s.jsonl"
        j.write_text(_json.dumps({
            "type": "assistant", "timestamp": "2026-07-02T01:00:01.000Z",
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "content": [{"type": "text", "text": "a"}],
                        "usage": {"input_tokens": "12k", "output_tokens": -5,
                                  "cache_read_input_tokens": None}}}) + "\n",
            encoding="utf-8")
        assert saikai._ctx_usage_from_jsonl(j) == (None, None)   # all-zero → skipped
    # last-record reader: a trailing valid-but-non-dict JSON line ([] / "x") is
    # NOT a record — returning it made _needs_attention AttributeError on .get()
    # (killed --table, broke every TUI refresh). (#audit-codex-lastrec)
    with _tf.TemporaryDirectory() as td:
        j = _P(td) / "t.jsonl"
        j.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n'
                     '[]\n', encoding="utf-8")
        assert saikai._read_last_jsonl_record(j) is None
        assert saikai._needs_attention(
            {"id": "s1", "mtime": 0.0, "jsonl_path": str(j)}, {}) is False
        j.write_text('"just a string"\n', encoding="utf-8")
        assert saikai._read_last_jsonl_record(j) is None
    # tab_label: newline/ANSI in a user-derived title must not corrupt the tab bar
    lbl = st.tab_label("evil\ntitle \x1b[2Jx", "busy")
    assert "\n" not in lbl and "\x1b" not in lbl and "evil title" in lbl
    assert st.tab_label(None, "idle") == "= agent"
    # rekey collision: never orphan an already-registered pane
    m = st.LiveSessionManager(max_live=4)
    a, b = object(), object()
    m.register("parent", a)
    m.register("child", b)
    m.rekey("parent", "child")
    assert m.get("child") is b and m.get("parent") is a, \
        "rekey onto an existing sid must be a no-op, not an overwrite"


def test_codex_round2_regressions():
    """Locks in the round-2 external-audit fixes (#audit-codex-*)."""
    import ast as _ast
    import json as _json
    import subprocess as _sp
    import tempfile as _tf
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P

    # 1. duplicate method definitions silently shadow the earlier one (a dup
    # on_descendant_focus turned the header-skip baseline into dead code).
    # Generic net: NO class in any saikai module may define a method twice.
    for mod in ("saikai.py", "saikai_terminal.py", "saikai_mirror.py"):
        tree = _ast.parse((_P(__file__).parent.parent / mod).read_text(
            encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                seen: dict = {}
                for item in node.body:
                    if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        assert item.name not in seen, (
                            f"{mod}: class {node.name} defines {item.name!r} twice "
                            f"(lines {seen[item.name]} and {item.lineno}) — the "
                            f"second silently shadows the first")
                        seen[item.name] = item.lineno

    # 2. _session_surface_model: a valid-but-non-dict line must not abort the scan
    with _tf.TemporaryDirectory() as td:
        j = _P(td) / "s.jsonl"
        j.write_text("[]\n"
                     + _json.dumps({"entrypoint": "cli"}) + "\n"
                     + _json.dumps({"type": "assistant",
                                    "message": {"model": "claude-opus-4-8"}}) + "\n",
                     encoding="utf-8")
        assert saikai._session_surface_model(j) == ("cli", "claude-opus-4-8")

    # 3. main() treats a broken stdout pipe as a normal pipeline end.
    # The wrapper re-points FD 1 at devnull (so shutdown flush can't re-raise);
    # save/restore the real stdout FD or every later test print vanishes.
    _orig = saikai._main
    saikai._main = lambda: (_ for _ in ()).throw(BrokenPipeError())
    _saved_fd = os.dup(1)
    try:
        try:
            saikai.main()
            raised = None
        except SystemExit as e:
            raised = e.code
        assert raised == 0, f"BrokenPipeError must exit(0), got {raised!r}"
    finally:
        os.dup2(_saved_fd, 1)
        os.close(_saved_fd)
        saikai._main = _orig

    # 4. chronological sorts parse tz-aware: +09:00 vs Z must order by instant
    early_jst = "2026-01-01T00:30:00+09:00"     # = 2025-12-31T15:30:00Z
    late_z = "2025-12-31T16:00:00Z"
    assert saikai._iso_sort_key(early_jst) < saikai._iso_sort_key(late_z)
    rows = [{"id": "early-jst", "first_ts": early_jst},
            {"id": "late-z", "first_ts": late_z}]
    rows.sort(key=lambda s: saikai._iso_sort_key(s["first_ts"]), reverse=True)
    assert [r["id"] for r in rows] == ["late-z", "early-jst"], rows
    assert saikai._iso_sort_key(None) == saikai._TS_EPOCH

    # 5. preview staleness: an append that moves mtime by <1s must re-render
    with _tf.TemporaryDirectory() as td:
        cache = _P(td) / "p.txt"
        calls = []
        saikai._write_if_stale(cache, 1000.0, lambda: calls.append(1) or "v1")
        saikai._write_if_stale(cache, 1000.5, lambda: calls.append(1) or "v2")
        assert len(calls) == 2, "a 0.5s-newer transcript must refresh the cache"
        assert cache.read_text(encoding="utf-8") == "v2"
        saikai._write_if_stale(cache, 1000.5, lambda: calls.append(1) or "v3")
        assert len(calls) == 2, "an unchanged mtime must still hit the cache"

    # 6. custom-titles cache key includes size: a same-mtime rewrite is seen
    ct = saikai.CUSTOM_TITLES_FILE
    ct.parent.mkdir(parents=True, exist_ok=True)
    ct.write_text(_json.dumps({"sid": "old"}), encoding="utf-8")
    ns = ct.stat().st_mtime_ns
    assert saikai._load_custom_titles().get("sid") == "old"
    ct.write_text(_json.dumps({"sid": "newer!"}), encoding="utf-8")
    os.utime(ct, ns=(ns, ns))                    # spoof: same mtime, new size
    assert saikai._load_custom_titles().get("sid") == "newer!", \
        "a same-mtime different-size rewrite must invalidate the cache"
    ct.unlink()
    saikai._CUSTOM_TITLES_CACHE = None


def test_codex_round3_regressions():
    """Locks in the round-3 external-audit fixes (#audit-codex-*)."""
    import json as _json
    import tempfile as _tf
    import time as _time
    from pathlib import Path as _P

    # 2. non-dict JSONL lines must not abort any scanner (b2 child detection,
    # previews, edited files, changes all shared the hole)
    with _tf.TemporaryDirectory() as td:
        j = _P(td) / "s.jsonl"
        j.write_text(
            "[]\n"
            + _json.dumps({"type": "user", "cwd": "/w",
                           "timestamp": "2026-07-01T00:00:00.000Z",
                           "message": {"role": "user", "content": "hi"}}) + "\n"
            + _json.dumps({"type": "assistant",
                           "timestamp": "2026-07-01T00:00:01.000Z",
                           "message": {"role": "assistant", "content": [
                               {"type": "tool_use", "name": "Write",
                                "input": {"file_path": "/w/x.py"}},
                               {"type": "text", "text": "done"}]}}) + "\n",
            encoding="utf-8")
        assert saikai._first_cwd_from_jsonl(j) == "/w"
        assert saikai._first_ts_from_jsonl(j) == "2026-07-01T00:00:00.000Z"
        assert saikai._extract_edited_files(j) == ["x.py"]
        assert "done" in (saikai._last_assistant_text_from_jsonl(j) or "")

    # 3. a syntactically-valid but non-table config section must not crash _cfg
    _orig_cache = getattr(saikai, "_CONFIG_CACHE", None)
    try:
        saikai._CONFIG_CACHE = ({"display": 1}, saikai._CONFIG_CACHE[1]) \
            if isinstance(_orig_cache, tuple) else None
    except Exception:
        pass
    # direct shape check on the resolution logic (env unset -> config path)
    os.environ.pop("SAIKAI_TEST_SHAPE", None)
    _lc = saikai._load_config
    saikai._load_config = lambda: {"display": 1}
    try:
        assert saikai._cfg("display", "split_ratio", "SAIKAI_TEST_SHAPE",
                           0.5, float) == 0.5
    finally:
        saikai._load_config = _lc

    # 4. corrupt option/set files must not crash startup
    saikai.OPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    saikai.OPTIONS_FILE.write_text("[1]", encoding="utf-8")
    assert saikai._load_options() == {}
    saikai.OPTIONS_FILE.unlink()
    with _tf.TemporaryDirectory() as td:
        p = _P(td) / "set.json"
        p.write_text("123", encoding="utf-8")
        assert saikai._load_set(p) == set()
        p.write_text('["a", 1, null, "b"]', encoding="utf-8")
        saikai._invalidate_pref(p)
        assert saikai._load_set(p) == {"a", "b"}

    # 8. a FUTURE mtime is neither active nor recent
    now = _time.time()
    future = {"mtime": now + 86400}
    assert saikai._is_recent_now(future, now) is False
    assert saikai._is_active_now(future, now) is False
    assert saikai._is_recent_now({"mtime": now - 60}, now) is True


def test_self_audit_round4_regressions():
    """Self-audit findings (#audit-self-*): the third same-mtime cache instance,
    non-positive ctx-window override, CLI preview freshness, jitter slack."""
    import json as _json
    import tempfile as _tf
    import time as _time
    from pathlib import Path as _P

    # B. _pref_cached keys on (mtime_ns, size): a same-mtime rewrite is seen
    with _tf.TemporaryDirectory() as td:
        p = _P(td) / "favorite.json"
        p.write_text('["a"]', encoding="utf-8")
        ns = p.stat().st_mtime_ns
        assert saikai._load_set(p) == {"a"}
        p.write_text('["a","bb"]', encoding="utf-8")
        os.utime(p, ns=(ns, ns))                 # spoof: same mtime, new size
        assert saikai._load_set(p) == {"a", "bb"}, \
            "a same-mtime different-size rewrite must invalidate _pref_cached"

    # D. a 0/negative window override falls back instead of poisoning the gauge
    assert saikai._ctx_window_for(1000, override=-5) == 200_000
    assert saikai._ctx_window_for(1000, override=0) == 200_000
    assert saikai._ctx_window_for(1000, override="abc") == 200_000
    assert saikai._ctx_window_for(1000, override=500_000) == 500_000

    # K. CLI preview must not serve a cache older than the transcript
    import contextlib as _cl
    import io as _io
    with _tf.TemporaryDirectory() as td:
        pdir = _P(td) / ".claude" / "projects" / "-w"
        pdir.mkdir(parents=True)
        sid = "cccccccc-0000-4000-8000-000000000001"
        j = pdir / f"{sid}.jsonl"
        j.write_text(_json.dumps({
            "type": "user", "cwd": "/w", "timestamp": "2026-07-01T00:00:00.000Z",
            "message": {"role": "user", "content": "OLD content"}}) + "\n",
            encoding="utf-8")
        _saved_root = saikai.PROJECTS_ROOT
        saikai.PROJECTS_ROOT = pdir.parent
        try:
            saikai.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            stale = saikai.PREVIEW_DIR / f"{sid}.txt"
            stale.write_text("STALE PREVIEW", encoding="utf-8")
            os.utime(stale, (j.stat().st_mtime - 100, j.stat().st_mtime - 100))
            buf = _io.StringIO()
            with _cl.redirect_stdout(buf):
                saikai.preview_session(sid)
            out = buf.getvalue()
            assert "STALE PREVIEW" not in out, \
                "a cache older than the transcript must be re-rendered"
            assert "OLD content" in out, out[:200]
            stale.unlink(missing_ok=True)
        finally:
            saikai.PROJECTS_ROOT = _saved_root

    # J. clock-jitter slack: an mtime a FEW SECONDS ahead still reads recent,
    # a genuinely future one (restored backup) does not
    now = _time.time()
    assert saikai._is_recent_now({"mtime": now + 2}, now) is True
    assert saikai._is_recent_now({"mtime": now + 86400}, now) is False
    assert saikai._is_active_now({"mtime": now + 2}, now) is True


def test_no_unguarded_jsonl_record_loops():
    """Permanent net for the round-3 bug class: every per-line json.loads loop
    must isinstance-guard (or bind through a dict-checking helper) before
    attribute access — 12 of 18 external-audit findings were this one hole in
    different places. Heuristic: the guard must appear within the next 8 lines
    of the loads. (#audit-codex-nondict)"""
    import re as _re
    root = Path(__file__).parent.parent
    bad = []
    for mod in ("saikai.py", "saikai_terminal.py", "saikai_mirror.py",
                "saikai_provider.py"):
        lines = (root / mod).read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            m = _re.search(r"(\w+)\s*=\s*json\.loads\((line|ln)\b", ln)
            if not m:
                continue
            var = m.group(1)
            window = "\n".join(lines[i + 1:i + 9])
            if f"isinstance({var}, dict)" not in window:
                bad.append(f"{mod}:{i + 1} ({var})")
    assert not bad, ("per-line json.loads without a dict guard — a valid-but-"
                     f"non-dict line ([]/\"x\") will abort the scan: {bad}")


def test_memory_safety_presets_and_override():
    """The one-knob memory_safety maps to gate-threshold presets: 'on' == the old
    per-OS defaults (no behaviour change), 'off' loosens the headroom, 'strict'
    tightens it and hard-refuses — and an explicit granular knob still overrides
    the preset. (#mem-safety-preset)"""
    saved = {k: os.environ.get(k) for k in ("SAIKAI_MEM_SAFETY", "SAIKAI_MAX_MEM_LOAD")}
    try:
        for k in ("SAIKAI_MEM_SAFETY", "SAIKAI_MAX_MEM_LOAD"):
            os.environ.pop(k, None)
        # default / on == the platform default max-load, warn (not hard).
        assert saikai._mem_safety_mode() == "on"
        on = saikai._ram_gate_kwargs()
        assert on["max_load"] == saikai._DEFAULT_MAX_LOAD
        assert saikai._mem_safety_preset()["hard"] is False
        # off: no conservative headroom (very high caps, zero floors), still warn.
        os.environ["SAIKAI_MEM_SAFETY"] = "off"
        off = saikai._ram_gate_kwargs()
        assert off["max_load"] >= 200 and off["min_free_phys_pct"] == 0 and off["min_commit_mb"] == 0
        # strict: refuse earlier + hard stop.
        os.environ["SAIKAI_MEM_SAFETY"] = "strict"
        st = saikai._ram_gate_kwargs()
        assert st["max_load"] < saikai._DEFAULT_MAX_LOAD and st["min_free_phys_pct"] >= 15
        assert saikai._mem_safety_preset()["hard"] is True
        # a bogus value falls back to 'on'.
        os.environ["SAIKAI_MEM_SAFETY"] = "banana"
        assert saikai._mem_safety_mode() == "on"
        # explicit granular knob overrides the preset (even in off mode).
        os.environ["SAIKAI_MEM_SAFETY"] = "off"
        os.environ["SAIKAI_MAX_MEM_LOAD"] = "70"
        assert saikai._ram_gate_kwargs()["max_load"] == 70.0
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    test_hostile_inputs_degrade_instead_of_raising()
    print("PASS test_hostile_inputs_degrade_instead_of_raising")
    test_codex_round2_regressions()
    print("PASS test_codex_round2_regressions")
    test_codex_round3_regressions()
    print("PASS test_codex_round3_regressions")
    test_self_audit_round4_regressions()
    print("PASS test_self_audit_round4_regressions")
    test_no_unguarded_jsonl_record_loops()
    print("PASS test_no_unguarded_jsonl_record_loops")
    test_memory_safety_presets_and_override()
    print("PASS test_memory_safety_presets_and_override")
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
    test_ctx_window_model_capacity()
    print("PASS test_ctx_window_model_capacity")
    test_ctx_gauge_segment_formats_and_colours()
    print("PASS test_ctx_gauge_segment_formats_and_colours")
    test_lineage_sidecar_roundtrip()
    print("PASS test_lineage_sidecar_roundtrip")
    test_b2_step_sequence_orders_clear_after_confirm_and_idle()
    print("PASS test_b2_step_sequence_orders_clear_after_confirm_and_idle")
    test_extract_handoff_prompt_slices_new_session_block()
    print("PASS test_extract_handoff_prompt_slices_new_session_block")
    test_resolve_handoff_prompt_override()
    print("PASS test_resolve_handoff_prompt_override")
    test_last_assistant_text_from_jsonl_reads_tail()
    print("PASS test_last_assistant_text_from_jsonl_reads_tail")
    test_first_cwd_from_jsonl_scans_early_records()
    print("PASS test_first_cwd_from_jsonl_scans_early_records")
    test_first_ts_from_jsonl_scans_early_records()
    print("PASS test_first_ts_from_jsonl_scans_early_records")
    test_bind_cleared_child_falsifiable_detection()
    print("PASS test_bind_cleared_child_falsifiable_detection")
    test_bind_cleared_child_clear_ts_timezone_robust()
    print("PASS test_bind_cleared_child_clear_ts_timezone_robust")
