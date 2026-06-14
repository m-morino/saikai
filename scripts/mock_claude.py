"""A faithful stand-in for `claude`, used only by the deterministic
screenshot/GIF scripts.

It paints the real Claude Code UI — the welcome box, tool calls with the `⎿`
result connector, and the bottom prompt box — into the PTY so the split-live
pane shows a convincing screen WITHOUT launching a real session: no auth, no real
history, no enterprise token, no API call, nothing to leak. Then it blocks until
the pane kills it.

The content is the fixture's fictional task (webapp → "fix the flaky auth token
refresh test", cwd /home/demo/work/webapp). Modeled on Claude Code 2.x; if a
glyph/color/line drifts from the current CLI, adjust the lines below to match.
"""
import sys
import time

# Claude Code palette (24-bit): the banner + action bullets use Claude's
# terracotta accent; tips/results are dim grey, prompts near-white.
ACCENT = "\x1b[38;2;215;119;87m"     # Claude "#d77757"
DIM = "\x1b[38;2;136;136;136m"
WHITE = "\x1b[38;2;230;230;230m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

W = 58                                # inner width of the rounded boxes

# OSC-0 title: the leading glyph is what saikai's status probe reads (idle).
sys.stdout.write("\x1b]0;✳ webapp\x07")


def boxline(plain: str, style: str = "") -> str:
    """One rounded-box row: '│ <plain> …padding… │', padded by the VISIBLE length
    of `plain` (style codes add no width)."""
    pad = max(0, W - len(plain) - 1)
    body = f"{style}{plain}{RESET if style else ''}"
    return f"{ACCENT}│{RESET} {body}{' ' * pad}{ACCENT}│{RESET}"


top = f"{ACCENT}╭{'─' * W}╮{RESET}"
bot = f"{ACCENT}╰{'─' * W}╯{RESET}"

prompt_ph = 'Try "run the full suite again"'
prompt_pad = max(0, W - len("> " + prompt_ph) - 1)

LINES = [
    # ── welcome box ──────────────────────────────────────────────────────────
    top,
    boxline("✻ Welcome back to Claude Code!", BOLD),
    boxline(""),
    boxline("/help for help, /status for your current setup", DIM),
    boxline(""),
    boxline("cwd: /home/demo/work/webapp", DIM),
    bot,
    "",
    # ── the resumed conversation ─────────────────────────────────────────────
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
    # ── bottom prompt box ────────────────────────────────────────────────────
    top,
    f"{ACCENT}│{RESET} {WHITE}>{RESET} {DIM}{prompt_ph}{RESET}{' ' * prompt_pad}{ACCENT}│{RESET}",
    bot,
    f"  {DIM}? for shortcuts{RESET}",
]

for ln in LINES:
    sys.stdout.write(ln + "\r\n")
sys.stdout.flush()

while True:
    time.sleep(3600)
