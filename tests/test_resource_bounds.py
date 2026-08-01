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


def test_handoff_prompt_forbids_identifier_truncation():
    """Regression for a real handoff defect (graded by an expert review): the built-in
    prompt listed identifiers to KEEP but never forbade abbreviating their VALUES, so a
    152-char Outlook message ID was "..."-elided in a checkpoint handoff — leaving the
    successor unable to run `mail delete` from the handoff alone and at risk of
    mis-deleting a near-identical ID (two drafts differing only in the tail). The prompt
    must now (a) widen the identifier category to opaque command-consumed handles,
    (b) forbid truncating/eliding an identifier VALUE, and (c) carve non-secret
    identifiers out of the 'refer to secrets by name/location' clause so the two rules
    don't fight. (#handoff-id-verbatim)"""
    p = saikai._B2_HANDOFF_PROMPT
    assert "VERBATIM and in FULL" in p, \
        "handoff prompt must require identifiers reproduced verbatim and in full"
    assert "elide an identifier value" in p, \
        "handoff prompt must forbid '...'-eliding an identifier value"
    assert "opaque handle a later command will consume" in p, \
        "identifier category must include opaque command-consumed handles (message/record IDs)"
    assert "This carve-out does NOT cover non-secret resource identifiers" in p, \
        "the secret/PII clause must carve out non-secret resource identifiers"
    assert "NEW SESSION PROMPT" in p, "the load-bearing reseed contract must survive the edit"


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


def test_option_labels_are_markup_safe():
    """OptionList prompts built from USER content (directory names in the
    Shift+F8 picker) must be Text objects: a bare-str prompt renders as markup,
    so a folder named "bad [/x] dir" raised MarkupError at LAYOUT time and
    crashed the whole app (reproduced). Static net: every Option( call in
    saikai.py wraps its label in Text(. (#audit-self-option-markup)"""
    import re as _re
    src = (Path(__file__).parent.parent / "saikai.py").read_text(encoding="utf-8")
    bare = [m.start() for m in _re.finditer(r"Option\((?!Text\()[a-z_]", src)]
    lines = [src[:pos].count("\n") + 1 for pos in bare]
    assert not lines, f"bare-str Option prompts (markup-unsafe): lines {lines}"


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



def test_parse_session_extracts_agent_lineage():
    """Agent lineage (#agent-lineage): parse_session pulls parentSessionId /
    agentId / isSidechain from the transcript into the session dict, and the
    disk-cache gate REJECTS an old cache lacking the field so upgrades repopulate."""
    import json, tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    child = d / "agent-abc.jsonl"
    child.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "timestamp": "2026-07-01T00:00:00.000Z", "cwd": "/w",
         "parentSessionId": "parent-123", "agentId": "abc", "isSidechain": True,
         "message": {"role": "user", "content": "subagent task"}},
        {"type": "user", "timestamp": "2026-07-01T00:01:00.000Z", "cwd": "/w",
         "message": {"role": "user", "content": "more"}},
    ]) + "\n", encoding="utf-8")
    p = saikai.parse_session(child)
    assert p is not None
    assert p.get("parent_session_id") == "parent-123", p
    assert p.get("agent_id") == "abc" and p.get("is_sidechain") is True, p
    plain = d / "plain.jsonl"
    plain.write_text(json.dumps(
        {"type": "user", "timestamp": "2026-07-01T00:00:00.000Z", "cwd": "/w",
         "message": {"role": "user", "content": "hi"}}) + "\n", encoding="utf-8")
    pp = saikai.parse_session(plain)
    assert pp.get("parent_session_id") == "" and pp.get("is_sidechain") is False, pp
    st = child.stat()
    stale = {"mtime": st.st_mtime, "size": st.st_size, "origin_cwd": "/w",
             "real_msgs": ["x"], "first_ts": "t"}
    cache_file = saikai.PARSED_DIR / (child.stem + ".json")
    saikai.PARSED_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(stale), encoding="utf-8")
    p2 = saikai.parse_session(child)
    assert p2.get("parent_session_id") == "parent-123", \
        "old cache without lineage must force a re-parse"

def test_crash_logging_records_a_crash_in_the_log():
    """An abrupt exit has to explain itself in saikai.log.

    saikai died mid-session (2026-07-31 13:39) and nothing on disk could say why: the
    log ended on an ordinary auto-reload line, Windows had filed no error report, and
    the one handler that did catch a UI crash printed the traceback to stderr only —
    where it scrolled away with the terminal. The log exists to answer "why did it
    close" and could not.

    Both hooks are asserted, because the two failures look identical in the log
    otherwise: a main-thread exception (the app dies) and a BACKGROUND-thread one (a
    pty reader / the mirror server / a reap dies while the app keeps running, which is
    the sneakier of the two). faulthandler covers the case where the interpreter itself
    goes down with no Python frame left to log from. (#crash-trail)"""
    import faulthandler
    import threading

    log = saikai.CACHE_DIR / "saikai.log"
    prev_hook, prev_thread_hook = sys.excepthook, threading.excepthook
    try:
        saikai._install_crash_logging()
        assert sys.excepthook is not prev_hook, "excepthook was not installed"

        try:
            raise RuntimeError("test-boom-main")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())      # what the interpreter would call

        def _raiser():
            raise ValueError("test-boom-thread")

        thread = threading.Thread(target=_raiser, name="pty-read-crashtest")
        thread.start()
        thread.join()

        text = log.read_text(encoding="utf-8", errors="replace")
        assert "CRASH (main): RuntimeError: test-boom-main" in text, text[-800:]
        assert "raise RuntimeError(\"test-boom-main\")" in text, \
            "the traceback body was not logged"
        assert ("CRASH (thread) in pty-read-crashtest: ValueError: test-boom-thread"
                in text), text[-800:]
        # A hard crash leaves no Python frame, so faulthandler has to be armed and its
        # file open — it cannot open one while the interpreter is already dying.
        assert faulthandler.is_enabled(), "faulthandler not enabled"
        assert (saikai.CACHE_DIR / "crash.log").exists(), "no crash.log for faults"
    finally:
        sys.excepthook, threading.excepthook = prev_hook, prev_thread_hook

def test_mode_reset_reaches_the_real_terminal_not_textuals_capture():
    """The disable sequences have to go to the TERMINAL, not to sys.stderr.

    For the whole life of the app Textual replaces sys.stderr with its own
    _PrintCapture, and that object's isatty() returns True deliberately ("Pretend
    we're a terminal"). So this function passed its own isatty guard and handed the
    mouse/paste/focus disables to app._print — the terminal got nothing. Every reset
    from inside app mode was a silent no-op: after an abrupt exit the shell was left
    with mouse tracking on, and a wheel scroll sprayed escape sequences at the
    prompt. That is what the terminal-death watchdog's "reset first" was supposed to
    prevent, and it ran in exactly the place the capture is installed.
    (#reset-real-stderr)"""
    import io as _io

    class _LyingCapture(_io.StringIO):
        """Textual's _PrintCapture: claims to be a terminal, isn't one."""
        def isatty(self):
            return True

    capture, real = _LyingCapture(), _LyingCapture()
    prev_err, prev_real, prev_platform = sys.stderr, sys.__stderr__, sys.platform
    try:
        # linux path: skip the win32 VT re-arm, which needs a real console handle
        saikai.sys.platform = "linux"
        sys.stderr = capture
        sys.__stderr__ = real
        saikai._reset_terminal_modes()
    finally:
        sys.stderr, sys.__stderr__ = prev_err, prev_real
        saikai.sys.platform = prev_platform

    assert capture.getvalue() == "", \
        "wrote to Textual's capture, where the terminal never sees it: %r" % (
            capture.getvalue(),)
    got = real.getvalue()
    for mode in ("\033[?1000l", "\033[?1002l", "\033[?1003l", "\033[?1004l",
                 "\033[?1006l", "\033[?2004l", "\033[?25h"):
        assert mode in got, "missing %r from the real terminal write: %r" % (mode, got)


def test_terminal_watchdog_logs_before_it_kills():
    """The watchdog exits with os._exit: nothing is written, nothing unwinds, no
    atexit runs. saikai died that way on 2026-07-31 13:39 and the log simply stopped
    on an ordinary line, so there was no way to tell this path from an external kill
    — and it is the one path that CAN kill a healthy session, on two consecutive
    failures of a process-tree walk under load.

    So it has to say so in the log, which is the only channel that survives here
    (stderr is Textual's capture and the screen is about to be gone). The near-miss
    line matters just as much: it is the only signal that a session survived by one
    poll. (#watchdog-trail)"""
    import threading
    import time as _time

    log = saikai.CACHE_DIR / "saikai.log"
    before = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    killed: list = []
    prev = (saikai.sys.platform, saikai._find_terminal_anchor,
            saikai._win_pid_index, saikai.subprocess.run, saikai.os._exit)
    seen = {"anchor": 0}

    def _fake_anchor(index, pid):
        seen["anchor"] += 1
        # arm (1st call), then one MISS, one recovery, then two misses -> kill
        return {1: 4242, 2: 0, 3: 4242}.get(seen["anchor"], 0)

    def _fake_exit(code):
        # The sentinel the test waits on is THIS one, not the taskkill: waiting on
        # the taskkill would let the finally-block restore the real os._exit while
        # the watchdog thread is still on its way to calling it — which would exit
        # the whole test run silently.
        killed.append(("exit", code))
        raise SystemExit(code)      # ends the watchdog thread, not the test process

    try:
        saikai.sys.platform = "win32"
        saikai._find_terminal_anchor = _fake_anchor
        # accepts strict=: the watchdog poll asks for the strict index now
        saikai._win_pid_index = lambda **kw: {os.getpid(): ("py.exe", 1)}
        saikai.subprocess.run = lambda *a, **kw: killed.append(("taskkill", a[0]))
        saikai.os._exit = _fake_exit
        saikai._start_terminal_watchdog(poll_sec=0.01)
        deadline = _time.monotonic() + 5.0
        while not any(k[0] == "exit" for k in killed)                 and _time.monotonic() < deadline:
            _time.sleep(0.02)
    finally:
        (saikai.sys.platform, saikai._find_terminal_anchor, saikai._win_pid_index,
         saikai.subprocess.run, saikai.os._exit) = prev

    added = (log.read_text(encoding="utf-8", errors="replace")[len(before):]
             if log.exists() else "")
    assert "watchdog: armed" in added, added[-600:]
    assert "watchdog: terminal ancestor back after 1 miss(es)" in added, added[-600:]
    assert "watchdog: no live terminal ancestor (miss 1/2)" in added, added[-600:]
    assert "watchdog: no live terminal ancestor (miss 2/2)" in added, added[-600:]
    assert "watchdog: terminal gone — resetting modes, reaping own tree" in added, \
        added[-600:]
    assert any(k[0] == "exit" for k in killed), "the watchdog never reached the kill"
    assert any(k[0] == "taskkill" for k in killed), "own tree was not reaped"

def test_snapshot_failure_is_not_read_as_a_dead_terminal():
    """A failed process snapshot and an empty one are OPPOSITE conclusions.

    _win_pid_index returned {} for every failure, _find_terminal_anchor({}) returns 0,
    and the watchdog reads 0 as "the terminal is gone" — so an enumeration failure was
    scored as a confirmed terminal death, and two in a row taskkilled saikai's own tree
    and os._exit(0)'d a healthy session with eleven live panes. The watchdog's own
    "inconclusive → reset the streak" branch could never fire, because nothing raised.

    Injected at the toolhelp BINDING, not by faking the ctypes module: the binding is
    built once and cached now (that is what fixes the argtypes race), so a fake module
    would never be consulted. The real PROCESSENTRY32 is kept and only the API is
    stubbed, so the struct handling stays honest. (#snapshot-failure-vs-empty)"""
    assert saikai._win_pid_index.__defaults__ == (False,), \
        "strict must default OFF so the arm check and pid verifier stay lenient"
    assert saikai._find_terminal_anchor({}, os.getpid()) == 0, \
        "precondition: an empty index yields the same 0 as 'terminal gone'"

    prev = saikai._th32
    try:
        # (1) Universal: no toolhelp at all (a non-Windows host, a ctypes failure).
        def _no_toolhelp():
            raise OSError("no windll here")
        saikai._th32 = _no_toolhelp
        assert saikai._win_pid_index() == {}, "lenient mode must stay quiet"
        try:
            saikai._win_pid_index(strict=True)
            raise AssertionError("strict mode swallowed a missing toolhelp")
        except saikai._SnapshotFailed as exc:
            assert "toolhelp unavailable" in str(exc), str(exc)
    finally:
        saikai._th32 = prev

    if sys.platform != "win32":
        print("  (win32-only API failure cases skipped)")
        return

    import types

    class _Fn:
        """A stand-in for a ctypes function: callable and attribute-assignable."""
        def __init__(self, ret):
            self.ret, self.restype, self.argtypes = ret, None, None

        def __call__(self, *a, **kw):
            return self.ret

    real_ctypes, _real_k32, P32 = saikai._th32()   # real struct, real ctypes

    def _binding(snap, first, nxt, last_err):
        k = types.SimpleNamespace()
        k.CreateToolhelp32Snapshot = _Fn(snap)
        k.Process32First = _Fn(first)
        k.Process32Next = _Fn(nxt)
        k.CloseHandle = _Fn(1)
        k.GetLastError = _Fn(last_err)
        return lambda: (real_ctypes, k, P32)

    CASES = {
        # snapshot handle refused outright
        "CreateToolhelp32Snapshot": _binding(0, 0, 0, 6),
        # walk ended for a reason OTHER than ERROR_NO_MORE_FILES(18) → truncated
        "truncated": _binding(7, 1, 0, 6),
        # complete-looking walk that does not contain us → incomplete by definition
        "missing our own pid": _binding(7, 1, 0, 18),
    }
    try:
        for expect, binding in CASES.items():
            saikai._th32 = binding
            assert saikai._win_pid_index() == {}, \
                "lenient mode must stay quiet for %r" % (expect,)
            try:
                saikai._win_pid_index(strict=True)
                raise AssertionError("strict mode swallowed: %s" % expect)
            except saikai._SnapshotFailed as exc:
                assert expect in str(exc), "%r not in %r" % (expect, str(exc))
    finally:
        saikai._th32 = prev


def test_abrupt_exit_leaves_the_alternate_screen():
    """An exit that bypasses Textual's teardown has to send ?1049l itself.

    The watchdog resets mouse/paste/focus and then os._exit(0)s, so nobody else takes
    the terminal off the alternate buffer: the user was dropped at a shell prompt drawn
    over the alt screen with their whole scrollback out of reach — "the terminal is
    left broken" — curable only by printf '\\e[?1049l' or a new tab.

    It stays OPT-IN: on a clean exit Textual has already left the buffer, and a second
    ?1049l also performs a DECRC, restoring a cursor position we never saved.
    (#leave-alt-on-abrupt-exit)"""
    import io as _io

    class _Tty(_io.StringIO):
        def isatty(self):
            return True

    def _emit(**kw):
        buf = _Tty()
        prev_real, prev_platform = sys.__stderr__, saikai.sys.platform
        try:
            saikai.sys.platform = "linux"     # skip the win32 VT re-arm
            sys.__stderr__ = buf
            saikai._reset_terminal_modes(**kw)
        finally:
            sys.__stderr__ = prev_real
            saikai.sys.platform = prev_platform
        return buf.getvalue()

    clean = _emit()
    assert "\033[?1049l" not in clean, \
        "a clean exit must not re-send ?1049l (its DECRC would move the cursor)"
    assert clean.endswith("\033[?25h")

    abrupt = _emit(leave_alt=True)
    assert "\033[?1049l" in abrupt, "an abrupt exit left the terminal on the alt screen"
    # Order matters: cursor visibility is per-buffer, so show it AFTER the switch back.
    assert abrupt.index("\033[?1049l") < abrupt.index("\033[?25h"), abrupt
    for mode in ("\033[?1003l", "\033[?1006l", "\033[?2004l"):
        assert mode in abrupt, mode

def test_every_ui_thread_subprocess_is_bounded():
    """A subprocess started from the UI thread must have a timeout.

    The Windows clipboard fallback had none — and it is reached exactly when
    OpenClipboard failed because ANOTHER process holds the clipboard (an RDP/VDI sync
    agent, Office, a browser), at which point clip.exe does the same OpenClipboard and
    blocks on the same holder. That call sits on the UI thread (on_mouse_up →
    _copy_text), so the Textual event loop stopped dead: no keystrokes, no repaints, no
    Ctrl+Q, leaving an external kill as the only way out — which then skips atexit and
    leaves the terminal in mouse mode.

    Asserted as a RULE over the source, not on one call site: the timeout kwarg is what
    keeps the class fixed as more helpers get added. The known-intentional blocking
    calls are listed explicitly, each with why. (#ui-thread-subprocess)"""
    import re

    root = Path(__file__).resolve().parent.parent
    # (file, line-content substring) -> reason it may block without a timeout
    ALLOWED = {
        "subprocess.run([*ed.split(), str(p)])":
            "the config editor runs inside app.suspend(); blocking IS the point",
        "subprocess.run(claude_argv, env=env)":
            "the resume handoff runs after the UI has exited",
        "subprocess.Popen([opener, str(p)]":
            "Popen without wait() does not block",
        "with subprocess.Popen(cmd, stdin=subprocess.PIPE":
            "bounded by communicate(timeout=...) below",
    }
    offenders = []
    for name in ("saikai.py", "saikai_terminal.py", "saikai_mirror.py"):
        src = (root / name).read_text(encoding="utf-8")
        lines = src.splitlines()
        for m in re.finditer(r"(?:subprocess|_sp)\.(?:run|Popen|check_output)\(", src):
            start = src.count("\n", 0, m.start())
            col = m.start() - (src.rindex("\n", 0, m.start()) + 1)
            if "#" in lines[start][:col]:
                continue            # a mention inside a comment, not a call
            call = "\n".join(lines[start:start + 9])
            depth = 0
            for i in range(call.index("("), len(call)):
                if call[i] == "(":
                    depth += 1
                elif call[i] == ")":
                    depth -= 1
                    if depth == 0:
                        call = call[:i + 1]
                        break
            flat = " ".join(call.split())
            if "timeout" in flat:
                continue
            if any(k in flat for k in ALLOWED):
                continue
            offenders.append("%s:%d  %s" % (name, start + 1, flat[:100]))
    assert not offenders, ("unbounded subprocess call(s) — each can freeze the UI "
                           "thread:\n  " + "\n  ".join(offenders))


def test_agent_kill_batch_runs_off_the_ui_thread():
    """Killing a batch of agents must not run in the modal's dismiss callback.

    That callback runs in Textual's message pump, and each _kill_agent_process spawns
    taskkill (up to its 10s timeout) plus a full process snapshot for the identity
    check — so confirming a parent row with several live children froze the app for the
    SUM of those: no repaints of the live panes, keystrokes queued, mirror frames
    stalled. _kill_agent_process's own docstring says it belongs off the UI thread.

    The worker is also TRACKED, not merely daemonised: a daemon thread dies with the
    interpreter, so quitting right after a kill would leave the target alive.
    (#ui-thread-subprocess)"""
    src = (Path(__file__).resolve().parent.parent / "saikai.py").read_text(
        encoding="utf-8")
    i = src.index("def _kill_agents(")
    body = src[i:i + 8000]
    j = body.index("def _do(")
    do_body = body[j:body.index("self.push_screen(KillAgentScreen")]
    assert "threading.Thread(" in do_body, \
        "_do still kills inline on the UI thread"
    assert "_track_reap" in do_body, \
        "the kill worker is not tracked, so a quick quit can cut it short"
    # The loop itself must live in the worker, not in _do.
    loop_at = do_body.index("for pid, ps, _t in targets:")
    work_at = do_body.index("def _work(")
    assert work_at < loop_at, "the kill loop is outside the worker function"
    # …and the toast has to be marshalled back to the UI thread.
    assert "call_from_thread" in do_body, "the report is not marshalled to the UI"


def test_new_session_scan_runs_off_the_ui_thread():
    """The new-session picker's candidate walk shells out to git and stats up to 40
    directories; doing it inline froze the picker and every live pane for up to the 5s
    git timeout before the modal even appeared. (#ui-thread-subprocess)"""
    src = (Path(__file__).resolve().parent.parent / "saikai.py").read_text(
        encoding="utf-8")
    i = src.index("def action_new_session(")
    body = src[i:src.index("def _new_session_candidates(")]
    assert "threading.Thread(" in body, "the scan still runs on the UI thread"
    assert "call_from_thread" in body, "the modal is not pushed from the UI thread"
    scan_at = body.index("self._new_session_candidates()")
    thread_at = body.index("def _scan(")
    assert thread_at < scan_at, "the walk is not inside the worker"
    assert "_new_session_scanning" in body, \
        "no re-entry guard: holding the key would pile up scans"

def test_windows_pid_guard_needs_more_than_an_image_name():
    """A recycled pid that happens to be a node must not pass as our agent.

    Nothing records procStart on Windows (census of this machine's live registry,
    2026-07-31: absent from every entry), so the guard fell back to the image name —
    and _CLAUDE_PROC_NAMES includes node.exe, one of the most common images on a
    developer box. MEASURED on the machine this was found on: the only node.exe running
    belonged to Adobe (node.exe < ccxprocess.exe < adobe desktop service.exe) and the
    old check accepted it, so a stale agent row plus Shift+K would have run
    `taskkill /PID <pid> /T /F` on Adobe's tree.

    claude.exe is unambiguous and still passes on its name. node.exe has to belong to
    us: an ancestor that is a claude process, or saikai itself (a pane child's parent IS
    our pid). Refusing is the safe direction — it loses only the ability to kill.
    (#pid-identity)"""
    own = os.getpid()
    #        pid: (image, ppid)
    INDEX = {
        own: ("python.exe", 900),
        900: ("pwsh.exe", 800),
        # a top-level claude, parented by a shell
        11: ("claude.exe", 900),
        # a claude-spawned agent that runs as node
        12: ("node.exe", 11),
        # a node saikai spawned itself (pane child)
        13: ("node.exe", own),
        # somebody else's node: VS Code / Adobe / an MCP server
        14: ("node.exe", 500),
        500: ("code.exe", 400),
        400: ("explorer.exe", 1),
        # an unrelated image that shares nothing
        15: ("ccxprocess.exe", 500),
        # a ppid cycle must not spin forever
        16: ("node.exe", 17),
        17: ("node.exe", 16),
    }
    prev_platform, prev_index = saikai.sys.platform, saikai._win_pid_index
    try:
        saikai.sys.platform = "win32"
        saikai._win_pid_index = lambda **kw: INDEX
        verdicts = {pid: saikai._proc_start_matches(pid, "") for pid in
                    (11, 12, 13, 14, 15, 16, 999999)}
    finally:
        saikai.sys.platform, saikai._win_pid_index = prev_platform, prev_index

    assert verdicts[11] is True, "a claude.exe must still verify"
    assert verdicts[12] is True, "a node under claude is ours"
    assert verdicts[13] is True, "a node under saikai itself is ours"
    assert verdicts[14] is False, "someone else's node.exe was accepted as our agent"
    assert verdicts[15] is False, "an unrelated image was accepted"
    assert verdicts[16] is False, "a ppid cycle must terminate and refuse"
    assert verdicts[999999] is False, "a pid the snapshot has never seen was accepted"

def test_concurrent_process_snapshots_do_not_corrupt_each_other():
    """Two threads walking the process list must not break each other's ctypes call.

    PROCESSENTRY32 used to be defined INSIDE _win_pid_index, so every call created a
    new class — while ctypes.windll.kernel32 is a process-wide cached object, which
    makes `Process32First.argtypes` shared state. Overlapping calls each rewrote
    argtypes to point at their own class and the loser raised

        ArgumentError: expected LP_PROCESSENTRY32 instance instead of LP_PROCESSENTRY32

    the same name, a different class. MEASURED with six concurrent walkers under
    process churn: 10864 of 10865 walks failed; after the fix, 0 of 1773.

    This was the real cause of the production failures behind the watchdog's
    "enumeration inconclusive" line (~once per 25 minutes of runtime): saikai walks the
    list from the UI thread for the live-session scan AND from the watchdog thread every
    8 seconds. Before strict mode, each such failure returned {} — which the watchdog
    read as a confirmed terminal death, two in a row being enough to reap a healthy
    session. (#toolhelp-argtypes-race)"""
    import threading as _threading

    if sys.platform != "win32":
        print("SKIP test_concurrent_process_snapshots_do_not_corrupt_each_other "
              "(toolhelp is Windows-only)")
        return

    # The binding is built once and shared, which is what makes the class identity —
    # and therefore argtypes — stable.
    assert saikai._th32() is saikai._th32(), "the toolhelp binding is rebuilt per call"

    errors: list = []
    walks = [0]
    lock = _threading.Lock()

    def _walk():
        for _ in range(25):
            try:
                idx = saikai._win_pid_index(strict=True)
            except Exception as exc:
                with lock:
                    errors.append(repr(exc))
                continue
            with lock:
                walks[0] += 1
                if os.getpid() not in idx:
                    errors.append("snapshot without our own pid")

    threads = [_threading.Thread(target=_walk, name="walk-%d" % i)
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, "concurrent snapshots failed %d time(s): %s" % (
        len(errors), errors[:3])
    assert walks[0] == 150, "expected 150 completed walks, got %d" % walks[0]


if __name__ == "__main__":
    test_hostile_inputs_degrade_instead_of_raising()
    print("PASS test_hostile_inputs_degrade_instead_of_raising")
    test_codex_round2_regressions()
    print("PASS test_codex_round2_regressions")
    test_codex_round3_regressions()
    print("PASS test_codex_round3_regressions")
    test_self_audit_round4_regressions()
    print("PASS test_self_audit_round4_regressions")
    test_option_labels_are_markup_safe()
    print("PASS test_option_labels_are_markup_safe")
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
    test_handoff_prompt_forbids_identifier_truncation()
    print("PASS test_handoff_prompt_forbids_identifier_truncation")
    test_last_assistant_text_from_jsonl_reads_tail()
    print("PASS test_last_assistant_text_from_jsonl_reads_tail")
    test_first_cwd_from_jsonl_scans_early_records()
    print("PASS test_first_cwd_from_jsonl_scans_early_records")
    test_parse_session_extracts_agent_lineage()
    print("PASS test_parse_session_extracts_agent_lineage")
    test_first_ts_from_jsonl_scans_early_records()
    print("PASS test_first_ts_from_jsonl_scans_early_records")
    test_bind_cleared_child_falsifiable_detection()
    print("PASS test_bind_cleared_child_falsifiable_detection")
    test_bind_cleared_child_clear_ts_timezone_robust()
    print("PASS test_bind_cleared_child_clear_ts_timezone_robust")
    test_crash_logging_records_a_crash_in_the_log()
    print("PASS test_crash_logging_records_a_crash_in_the_log")
    test_mode_reset_reaches_the_real_terminal_not_textuals_capture()
    print("PASS test_mode_reset_reaches_the_real_terminal_not_textuals_capture")
    test_terminal_watchdog_logs_before_it_kills()
    print("PASS test_terminal_watchdog_logs_before_it_kills")
    test_snapshot_failure_is_not_read_as_a_dead_terminal()
    print("PASS test_snapshot_failure_is_not_read_as_a_dead_terminal")
    test_abrupt_exit_leaves_the_alternate_screen()
    print("PASS test_abrupt_exit_leaves_the_alternate_screen")
    test_every_ui_thread_subprocess_is_bounded()
    print("PASS test_every_ui_thread_subprocess_is_bounded")
    test_agent_kill_batch_runs_off_the_ui_thread()
    print("PASS test_agent_kill_batch_runs_off_the_ui_thread")
    test_new_session_scan_runs_off_the_ui_thread()
    print("PASS test_new_session_scan_runs_off_the_ui_thread")
    test_windows_pid_guard_needs_more_than_an_image_name()
    print("PASS test_windows_pid_guard_needs_more_than_an_image_name")
    test_concurrent_process_snapshots_do_not_corrupt_each_other()
    print("PASS test_concurrent_process_snapshots_do_not_corrupt_each_other")
