#!/usr/bin/env python3
"""Unit tests for the terminal-death watchdog ancestor walk (saikai.py).

Covers _find_terminal_anchor — the one piece of watchdog logic whose
correctness determines whether the watchdog fires on the *right* process.
The thread/taskkill path is integration-verified separately (it only ever
targets os.getpid()'s own tree, so a wrong anchor can never hurt another
session; the worst a logic bug does is fire early/late on saikai itself).

Run:  uv run --no-project python tests/test_terminal_watchdog.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import saikai  # noqa: E402

anchor = saikai._find_terminal_anchor
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got}, want {want}")


# 1. wezterm → pwsh(tab) → cmd(saikai.cmd shim) → uv → python(self).
#    Anchor must be the tab pwsh, NOT the cmd shim (which orphans with us).
check("cmd-shim under pwsh tab", anchor({
    1000: ("wezterm-gui.exe", 1),
    1001: ("pwsh.exe", 1000),
    1002: ("cmd.exe", 1001),
    1003: ("uv.exe", 1002),
    1004: ("python.exe", 1003),
}, 1004), 1001)

# 2. wezterm → cmd(tab, saikai.cmd invoked directly) → uv → python(self).
#    Here cmd IS the tab shell, so it is the correct anchor.
check("cmd is the tab shell", anchor({
    1000: ("wezterm-gui.exe", 1),
    1001: ("cmd.exe", 1000),
    1002: ("uv.exe", 1001),
    1003: ("python.exe", 1002),
}, 1003), 1001)

# 3. Nested: pwsh(launcher) → wezterm → pwsh(tab) → uv → python(self).
#    Must anchor on the tab pwsh, never the launcher pwsh above the emulator
#    (the launcher survives a tab close → anchoring on it would never fire).
check("launcher pwsh above emulator ignored", anchor({
    900:  ("pwsh.exe", 1),
    1000: ("wezterm-gui.exe", 900),
    1001: ("pwsh.exe", 1000),
    1002: ("uv.exe", 1001),
    1003: ("python.exe", 1002),
}, 1003), 1001)

# 4. Headless (scheduled task / test runner): no shell ancestor → disabled.
check("headless → 0 (disabled)", anchor({
    500:  ("services.exe", 1),
    1002: ("uv.exe", 500),
    1003: ("python.exe", 1002),
}, 1003), 0)

# 5. Cycle in the chain must not hang and must terminate cleanly.
check("cycle-safe", anchor({
    1: ("a.exe", 2),
    2: ("b.exe", 1),
}, 1), 0)

# 6. Broken chain (parent pid missing from snapshot) → 0, no crash.
check("broken chain → 0", anchor({
    1003: ("python.exe", 1002),
}, 1003), 0)

# 7. bash `saikai` wrapper: wezterm → bash(tab) → uv → python(self).
check("bash wrapper tab", anchor({
    1000: ("wezterm-gui.exe", 1),
    1001: ("bash.exe", 1000),
    1002: ("uv.exe", 1001),
    1003: ("python.exe", 1002),
}, 1003), 1001)

# 8. Guard: never anchor on start_pid itself even if it is shell-named;
#    pick the parent tab shell instead.
check("self is shell → parent anchor", anchor({
    1000: ("wezterm-gui.exe", 1),
    1001: ("pwsh.exe", 1000),
    1003: ("pwsh.exe", 1001),
}, 1003), 1001)

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
