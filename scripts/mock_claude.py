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
  argv[2] = the public cwd to display (the real subprocess cwd is a temp repo;
            never shown, so no path leaks).

State is driven exactly as saikai_terminal.classify_pty_status reads it: the OSC
title's first glyph (spinner = busy, ✳ = idle) and an on-screen permission
prompt ("Do you want…", "❯ 1."). Then it blocks until the pane kills it.
"""
import shutil
import sys
import time

scenario = sys.argv[1] if len(sys.argv) > 1 else "idle"
cwd = sys.argv[2] if len(sys.argv) > 2 else "/home/demo/work/webapp"

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
    return [
        f"{ACCENT}{LOGO[0]:<9}{RESET}  {BOLD}{WHITE}Claude Code{RESET} {DIM}v2.1.177{RESET}",
        f"{ACCENT}{LOGO[1]:<9}{RESET}  {WHITE}Opus 4.8 (1M context){RESET}{model_tail}",
        f"{ACCENT}{LOGO[2]:<9}{RESET}  {DIM}{cwd}{RESET}",
        "",
        f" {DIM}▎ Using Opus 4.8 (1M context) (from .claude/settings.json) · /model{RESET}",
        "",
    ]


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
    time.sleep(5.0)            # keep working while the demo opens/flips panes
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
else:
    # ── The faithful auth-fix transcript; settles idle (✳). ───────────────────
    title("✳ webapp")
    prompt_ph = 'Try "run the full suite again"'
    emit(header(f" {DIM}with max effort{RESET}") + [
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
        rule,
        f"{WHITE}❯{RESET} {DIM}{prompt_ph}{RESET}",
        rule,
        f"  {GOLD}⏵⏵ auto mode on{RESET} {DIM}(shift+tab to cycle) · ← for agents{RESET}",
    ])

while True:
    time.sleep(3600)
