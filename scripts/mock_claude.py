"""A stand-in for `claude` used ONLY by scripts/make_screenshots.py.

Paints a Claude-Code-like welcome + transcript into the PTY so the split-live
pane screenshot shows realistic content without launching (or leaking) a real
session. Then blocks until the pane kills it.
"""
import sys
import time

CYAN = "\x1b[36m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
ORANGE = "\x1b[38;5;208m"
GREEN = "\x1b[32m"
RESET = "\x1b[0m"

# OSC-0 title: the leading "✳" is what saikai's status probe reads as "idle".
sys.stdout.write("\x1b]0;✳ webapp\x07")

LINES = [
    f"{ORANGE}╭{'─' * 58}╮{RESET}",
    f"{ORANGE}│{RESET} ✳ {BOLD}Welcome back to Claude Code!{RESET}{' ' * 27}{ORANGE}│{RESET}",
    f"{ORANGE}│{RESET}{' ' * 58}{ORANGE}│{RESET}",
    f"{ORANGE}│{RESET}   {DIM}/help for help, /status for your current setup{RESET}{' ' * 8}{ORANGE}│{RESET}",
    f"{ORANGE}│{RESET}{' ' * 58}{ORANGE}│{RESET}",
    f"{ORANGE}│{RESET}   {DIM}cwd: ~/code/webapp{RESET}{' ' * 37}{ORANGE}│{RESET}",
    f"{ORANGE}╰{'─' * 58}╯{RESET}",
    "",
    f"{DIM}>{RESET} Fix the flaky auth token refresh test",
    "",
    f"{GREEN}●{RESET} I'll look at the failing test first to understand the race.",
    "",
    f"{GREEN}●{RESET} {BOLD}Read{RESET}(tests/test_auth.py)",
    f"  ⎿  Read 214 lines",
    "",
    f"{GREEN}●{RESET} {BOLD}Bash{RESET}(pytest tests/test_auth.py -x -q)",
    f"  ⎿  1 failed, 23 passed — token refresh raced the clock mock",
    "",
    f"{GREEN}●{RESET} The test froze {CYAN}time.monotonic{RESET} but the refresh path reads",
    f"  {CYAN}datetime.now(){RESET}. Pinning both to the same fake clock:",
    "",
    f"{GREEN}●{RESET} {BOLD}Update{RESET}(tests/test_auth.py)",
    f"  ⎿  Updated tests/test_auth.py with 2 additions and 1 removal",
    "",
    f"{ORANGE}╭{'─' * 58}╮{RESET}",
    f"{ORANGE}│{RESET} > {DIM}Try \"run the full suite again\"{RESET}{' ' * 25}{ORANGE}│{RESET}",
    f"{ORANGE}╰{'─' * 58}╯{RESET}",
]

for ln in LINES:
    sys.stdout.write(ln + "\r\n")
sys.stdout.flush()

while True:
    time.sleep(3600)
