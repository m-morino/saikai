"""A faithful stand-in for `claude`, used only by the deterministic
screenshot/GIF scripts.

It paints the real Claude Code 2.x UI into the PTY so the split-live pane looks
like a real session WITHOUT launching one (no auth, history, token, or API
call). Two scenarios drive the demo:

  argv[1] = "idle"      (default) the faithful auth-fix transcript, settles idle
                        (OSC title "✳" -> saikai marks the pane "=").
  argv[1] = "needs-you" works briefly with a SPINNER title (-> saikai "~ busy"),
                        then re-titles to "✳" and prints a permission prompt
                        (-> saikai "? waiting"), so the LIST marker animates
                        ~ -> ? while the demo flips between panes.
  argv[1] = "checkpoint" the context-lifecycle pane. Settles idle, then READS
                        stdin (the bracketed-paste input saikai injects) and
                        drives the REAL b2 checkpoint machine honestly:
                          • on the injected handoff prompt: spinner title (busy)
                            ~1s + a "writing handoff…" turn, then settle idle —
                            so b2 sees busy→idle and advances to extract;
                          • on "/clear": MINT a fresh <uuid>.jsonl in the project
                            dir (so b2's detect_child binds it) with a LOW usage
                            (the reseeded pane reads GREEN), and repaint a lean
                            session. b2 then pastes the reseed prompt in.
  argv[2] = the public cwd to display (the real subprocess cwd is a temp repo;
            never shown, so no path leaks).
  argv[3] = (checkpoint only) the project dir to mint the post-/clear child
            transcript into — saikai's detect_child watches it.
  argv[4] = (checkpoint only) the parent session id (only used to avoid colliding
            the minted child filename with the parent).

State is driven exactly as saikai_terminal.classify_pty_status reads it: the OSC
title's first glyph (spinner = busy, ✳ = idle) and an on-screen permission
prompt ("Do you want…", "❯ 1."). The idle/needs-you scenarios then block until
the pane kills them; checkpoint runs a stdin-driven loop (see below).
"""
import os
import shutil
import sys
import threading
import time

scenario = sys.argv[1] if len(sys.argv) > 1 else "idle"
cwd = sys.argv[2] if len(sys.argv) > 2 else "/home/demo/work/webapp"
project_dir = sys.argv[3] if len(sys.argv) > 3 else ""
parent_sid = sys.argv[4] if len(sys.argv) > 4 else ""

# Claude Code palette (24-bit).
ACCENT = "\x1b[38;2;215;119;87m"     # terracotta "#d77757"
DIM = "\x1b[38;2;136;136;136m"
WHITE = "\x1b[38;2;230;230;230m"
GOLD = "\x1b[38;2;212;170;100m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

WIDTH = max(24, min(shutil.get_terminal_size((76, 24)).columns, 200))
rule = f"{DIM}{'─' * WIDTH}{RESET}"
LOGO = [" ▐▛███▜▌", "▝▜█████▛▘", "  ▘▘ ▝▝"]      # transcribed from real Claude Code


def title(s):
    sys.stdout.write(f"\x1b]0;{s}\x07")
    sys.stdout.flush()


def emit(lines):
    for ln in lines:
        sys.stdout.write(ln + "\r\n")
    sys.stdout.flush()


def header(model_tail):
    # Matches the REAL Claude Code startup (captured 2026-06-18, v2.1.181): logo +
    # version, "Opus 4.8 (1M context) with max effort", the cwd, then two blanks —
    # NO "Using … from settings.json /model" line (that was never in the real UI).
    return [
        f"{ACCENT}{LOGO[0]:<9}{RESET}  {BOLD}{WHITE}Claude Code{RESET} {DIM}v2.1.181{RESET}",
        f"{ACCENT}{LOGO[1]:<9}{RESET}  {WHITE}Opus 4.8 (1M context){RESET} {DIM}with max effort{RESET}{model_tail}",
        f"{ACCENT}{LOGO[2]:<9}{RESET}  {DIM}{cwd}{RESET}",
        "",
        "",
    ]


def prompt_box(placeholder):
    """The faithful idle input box (the ❯ prompt). Its presence with a ✳ title
    is what saikai classifies as idle/ready — and what makes the pane look like
    claude waiting at its prompt for the next turn."""
    _foot_l = "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"
    _pad = " " * max(2, WIDTH - len(_foot_l) - len("◈ max · /effort"))
    return [
        rule,
        f"{WHITE}❯{RESET} {DIM}{placeholder}{RESET}",
        rule,
        (f"  {GOLD}⏵⏵ auto mode on{RESET} {DIM}(shift+tab to cycle) · ← for agents{RESET}"
         f"{_pad}{DIM}◈ max · /effort{RESET}"),
    ]


# ── Bracketed paste: saikai injects multi-line input wrapped in ESC[200~ … ESC
# [201~ (it only does so once it sees us enable ?2004h — real claude does too).
# We enable the mode, then a background thread drains stdin into a shared buffer
# so the main loop can scan for the handoff prompt / "/clear" WITHOUT blocking
# the timed busy→idle transition. Strip the paste markers when interpreting.
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
_in_buf = []
_in_lock = threading.Lock()


def enable_bracketed_paste():
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.flush()


def _strip_paste_markers(s):
    return s.replace(_PASTE_START, "").replace(_PASTE_END, "")


def _stdin_reader():
    """Drain the PTY's stdin into _in_buf. Blocking reads in a daemon thread so
    the main loop polls the accumulated text. Exits quietly on EOF/close (the
    pane kills us at teardown)."""
    try:
        stream = sys.stdin.buffer
    except Exception:
        return
    while True:
        try:
            chunk = stream.read(1)          # 1 byte: deliver paste+CR promptly
        except Exception:
            return
        if not chunk:
            return
        with _in_lock:
            _in_buf.append(chunk)


def consumed_text():
    """All stdin seen so far, paste-markers stripped (for substring matching)."""
    with _in_lock:
        raw = b"".join(_in_buf)
    return _strip_paste_markers(raw.decode("utf-8", "replace"))


def mint_child_session(pdir, public_cwd, exclude_sid):
    """Mint the fresh <uuid>.jsonl that `/clear` produces, exactly the shape
    saikai's _bind_cleared_child falsifiably binds: an early `mode` record, then
    an `attachment` carrying the PUBLIC cwd + a current UTC timestamp (post-dating
    the clear), then a lean assistant turn with a LOW usage so the reseeded pane's
    gauge reads GREEN. Returns the child sid, or '' if it couldn't be written."""
    import json
    import uuid
    from datetime import datetime, timezone
    if not pdir:
        return ""
    child_sid = str(uuid.uuid4())
    while child_sid == exclude_sid:
        child_sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recs = [
        {"type": "mode", "sessionId": child_sid},
        {"type": "file-history-snapshot", "timestamp": now},
        {"type": "attachment", "cwd": public_cwd, "timestamp": now,
         "sessionId": child_sid},
        {"type": "assistant", "timestamp": now, "cwd": public_cwd,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "(fresh session)"}],
                     "usage": {"input_tokens": 1900,
                               "cache_read_input_tokens": 9000,
                               "cache_creation_input_tokens": 1200,
                               "output_tokens": 120}}},
    ]
    try:
        path = os.path.join(pdir, child_sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    except OSError:
        return ""
    return child_sid


if scenario == "needs-you":
    # ── Session that is WORKING, then needs your call. Spinner title => busy. ──
    title("⠹ api-server")
    emit(header("") + [
        f"{DIM}>{RESET} Profile GET /orders and fix the line-item N+1 queries",
        "",
        f"{ACCENT}●{RESET} {BOLD}Read{RESET}(api/orders.py)",
        f"  {DIM}⎿  Read 168 lines{RESET}",
        "",
        f"{ACCENT}●{RESET} Each order re-queries its line items in a loop — I'll",
        f"  batch them into one joined load.",
        "",
        f"{ACCENT}●{RESET} {BOLD}Update{RESET}(api/orders.py)",
        f"  {GOLD}Working…{RESET} {DIM}(esc to interrupt){RESET}",
    ])
    time.sleep(2.5)            # brief "working" then settle to the prompt (the demo
                               # snaps the ? end-state, so the prompt must be up early)
    # Re-title to ✳ (no longer a spinner) + show a permission prompt => waiting.
    title("✳ api-server")
    emit([
        "",
        f"{ACCENT}●{RESET} This rewrites a query on a hot path. Apply the edit?",
        "",
        f"  {WHITE}Do you want to apply this change to {BOLD}orders.py{RESET}{WHITE}?{RESET}",
        f"  {ACCENT}❯ 1.{RESET} {WHITE}Yes{RESET}",
        f"    {DIM}2. No, explain the change first{RESET}",
    ])
    # HOLD at the permission prompt so the list marker stays ? (waiting) — without
    # this the script exits, the pane dies, and the marker falls back to = (idle).
    while True:
        time.sleep(3600)
elif scenario == "fresh":
    # ── A freshly reseeded session: lean context, idle at its prompt. Used to
    # show the GREEN gauge on the post-/clear child (which has a low usage). ───
    title("✳ billing")
    emit(header("") + [
        f"{DIM}>{RESET} Continue the billing-module refactor — extract invoices first",
        "",
        f"{ACCENT}●{RESET} Picking up from the handoff: split billing behind the",
        f"  facade, invoices first, keep the public API stable.",
        "",
        f"{ACCENT}●{RESET} {BOLD}Read{RESET}(billing/legacy.py)",
        f"  {DIM}⎿  Read 3,084 lines{RESET}",
        "",
        f"{ACCENT}●{RESET} Starting with the invoice builder — it has the fewest",
        f"  cross-calls. I'll move it to billing/invoices.py.",
        "",
    ] + prompt_box('Try "run the billing tests"'))
    while True:
        time.sleep(3600)
elif scenario == "checkpoint":
    # ── The context-lifecycle pane: a GROWN session sitting idle at its prompt,
    # then driven by saikai's REAL b2 machine via injected input. ──────────────
    title("✳ billing")
    emit(header("") + [
        f"{DIM}>{RESET} Walk the call graph first; don't move anything until we agree",
        "",
        f"{ACCENT}●{RESET} {BOLD}Read{RESET}(billing/legacy.py)",
        f"  {DIM}⎿  Read 3,084 lines{RESET}",
        "",
        f"{ACCENT}●{RESET} Invoices, proration, and tax are tangled in one file. The",
        f"  seams are: invoice build, proration math, and tax lookup.",
        "",
        f"{ACCENT}●{RESET} {BOLD}Grep{RESET}(BillingFacade)",
        f"  {DIM}⎿  Found 18 call sites across 6 files{RESET}",
        "",
        f"{ACCENT}●{RESET} Agreed — split behind the facade, invoices first. Nothing",
        f"  moved yet.",
        "",
    ] + prompt_box('Try "extract invoices first"'))
    # Real claude enables bracketed paste at its prompt; do the same so saikai
    # wraps the multi-line handoff it injects (else newlines submit line-by-line).
    enable_bracketed_paste()
    threading.Thread(target=_stdin_reader, daemon=True).start()

    # b2 step 1: it injects the handoff prompt. We detect a stable substring of
    # saikai's built-in handoff prompt (its FIRST sentence), go BUSY (spinner
    # title) ~1s with a "writing handoff…" turn, then settle idle (✳). b2's
    # await_handoff_idle sees busy→idle and advances to extract the NEW SESSION
    # PROMPT — which it reads from the PARENT transcript (the fixture), not from
    # us, so we only need to drive the busy→idle status here.
    _HANDOFF_MARK = "Wrap up THIS session"
    handoff_seen = clear_seen = False
    while not handoff_seen:
        if _HANDOFF_MARK in consumed_text():
            handoff_seen = True
            break
        time.sleep(0.1)

    title("⠹ billing")                     # spinner ⇒ busy
    emit([
        "",
        f"{ACCENT}●{RESET} {BOLD}Writing handoff…{RESET} {DIM}(esc to interrupt){RESET}",
    ])
    time.sleep(1.0)                        # stay busy ~1s so b2 SEES it work
    # Settle idle. CLEAR the screen first: the busy line above carries
    # "(esc to interrupt)", which saikai's classifier treats as a body busy-marker
    # — leaving it on screen would keep the pane "busy" forever even after the ✳
    # title. Repaint a clean idle frame with NO busy markers so b2 advances.
    sys.stdout.write("\x1b[2J\x1b[H")
    title("✳ billing")                     # ✳ ⇒ idle ⇒ b2 advances to extract
    emit(header("") + [
        f"{DIM}>{RESET} Wrap up this session so a new one can resume the work",
        "",
        f"{ACCENT}●{RESET} Handoff ready — short summary above, and a fenced",
        f"  {BOLD}NEW SESSION PROMPT{RESET} block with the resume instructions.",
        "",
    ] + prompt_box('Try "looks good"'))

    # b2 then shows the confirm modal; on Enter it pastes "/clear". Watch for it.
    while not clear_seen:
        if "/clear" in consumed_text():
            clear_seen = True
            break
        time.sleep(0.1)

    # /clear mints a brand-new child session. Reproduce that: write the child
    # JSONL b2's detect_child binds, then repaint a fresh, lean session. b2 will
    # paste the extracted NEW SESSION PROMPT in as the first turn.
    child_sid = mint_child_session(project_dir, cwd, parent_sid)
    title("✳ billing")
    sys.stdout.write("\x1b[2J\x1b[H")      # clear screen, home — a fresh session
    emit(header("") + [
        f" {DIM}▎ Context cleared. Starting a fresh session.{RESET}",
        "",
    ] + prompt_box('Try "extract invoices first"'))
    enable_bracketed_paste()               # fresh prompt re-enables paste mode

    # Idle from here; the pane kills us at teardown. (We've already minted the
    # child and repainted lean — b2's reseed paste lands at this prompt.)
    while True:
        time.sleep(3600)
else:
    # ── The faithful auth-fix transcript; settles idle (✳). ───────────────────
    title("✳ webapp")
    emit(header("") + [
        f"{DIM}>{RESET} Fix the flaky auth token refresh test",
        "",
        f"{ACCENT}●{RESET} I'll read the failing test first to understand the race.",
        "",
        f"{ACCENT}●{RESET} {BOLD}Read{RESET}(tests/test_auth.py)",
        f"  {DIM}⎿  Read 214 lines{RESET}",
        "",
        f"{ACCENT}●{RESET} {BOLD}Bash{RESET}(pytest tests/test_auth.py -x -q)",
        f"  {DIM}⎿  1 failed, 23 passed in 0.41s{RESET}",
        f"  {DIM}   FAILED test_auth.py::test_refresh_at_expiry_boundary{RESET}",
        "",
        f"{ACCENT}●{RESET} The test froze {BOLD}time.monotonic{RESET} but the refresh path",
        f"  reads {BOLD}datetime.now(){RESET} — pinning both to one fake clock.",
        "",
        f"{ACCENT}●{RESET} {BOLD}Update{RESET}(tests/test_auth.py)",
        f"  {DIM}⎿  Updated tests/test_auth.py with 2 additions and 1 removal{RESET}",
        "",
    ] + prompt_box('Try "run the full suite again"'))

    while True:
        time.sleep(3600)
