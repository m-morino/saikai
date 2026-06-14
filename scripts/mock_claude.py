"""A faithful stand-in for `claude`, used only by the deterministic
screenshot/GIF scripts.

It paints the real Claude Code startup screen — the terracotta logo, the
`Claude Code vX.Y.Z` / model / cwd header, the tool-call transcript with the
`⎿` result connector, and the rule-bounded `❯` input — into the PTY so the
split-live pane shows a convincing screen WITHOUT launching a real session: no
auth, no real history, no enterprise token, no API call, nothing to leak. Then
it blocks until the pane kills it.

The logo glyphs and layout are transcribed from a real Claude Code 2.x startup.
The content is the fixture's fictional task (webapp → "fix the flaky auth token
refresh test", cwd /home/demo/work/webapp); the model line is kept neutral (no
account plan), and the real CLI's personal statusline is intentionally omitted.
If a glyph/color/line drifts from the current CLI, adjust the lines below.
"""
import shutil
import sys
import time

# Claude Code palette (24-bit): the logo + action bullets use Claude's
# terracotta accent; tips/results/rules are dim grey, headings near-white.
ACCENT = "\x1b[38;2;215;119;87m"     # Claude "#d77757"
DIM = "\x1b[38;2;136;136;136m"
WHITE = "\x1b[38;2;230;230;230m"
GOLD = "\x1b[38;2;212;170;100m"      # auto-accept / mode indicator
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

# Full-width horizontal rules frame the input, like the real CLI. Size them to
# the pane's PTY (falls back to a sane width if the size can't be read).
WIDTH = shutil.get_terminal_size((76, 24)).columns
WIDTH = max(24, min(WIDTH, 200))
rule = f"{DIM}{'─' * WIDTH}{RESET}"

# The auto-accept indicator + effort badge below the input, the badge
# right-aligned to the pane. The real CLI's personal statusline (username,
# cost, custom segments) is intentionally NOT reproduced for a public demo.
_auto = "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"
_effort = "◈ max · /effort"
_gap = max(3, WIDTH - 2 - len(_auto) - len(_effort))
autoline = (f"  {GOLD}⏵⏵ auto mode on{RESET} "
            f"{DIM}(shift+tab to cycle) · ← for agents{RESET}"
            f"{' ' * _gap}{DIM}{_effort}{RESET}")

# OSC-0 title: the leading glyph is what saikai's status probe reads (idle).
sys.stdout.write("\x1b]0;✳ webapp\x07")

# The real startup logo (terracotta), transcribed glyph-for-glyph. Left-pad
# each row to the widest one so the three header lines align cleanly.
# Centre each row on the widest (the 9-wide body) so the creature is symmetric:
# head (7) gets 1 leading space, feet (5) get 2 — without them the head sits a
# column left of the body and the mascot looks skewed.
LOGO = [" ▐▛███▜▌", "▝▜█████▛▘", "  ▘▘ ▝▝"]
placeholder = 'Try "run the full suite again"'

LINES = [
    # ── startup header: logo + version + model + cwd (no box — matches real) ──
    f"{ACCENT}{LOGO[0]:<9}{RESET}  {BOLD}{WHITE}Claude Code{RESET} {DIM}v2.1.177{RESET}",
    f"{ACCENT}{LOGO[1]:<9}{RESET}  {WHITE}Opus 4.8 (1M context){RESET} {DIM}with max effort{RESET}",
    f"{ACCENT}{LOGO[2]:<9}{RESET}  {DIM}/home/demo/work/webapp{RESET}",
    "",
    f" {DIM}▎ Using Opus 4.8 (1M context) (from .claude/settings.json) · /model{RESET}",
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
    # ── input: two rules around the ❯ prompt (matches real Claude Code) ───────
    rule,
    f"{WHITE}❯{RESET} {DIM}{placeholder}{RESET}",
    rule,
    autoline,
]

for ln in LINES:
    sys.stdout.write(ln + "\r\n")
sys.stdout.flush()

while True:
    time.sleep(3600)
