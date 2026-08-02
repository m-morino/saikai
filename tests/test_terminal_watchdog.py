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


def test_find_terminal_anchor():
    """The outermost tab shell below the emulator is the only valid anchor."""
    cases = (
        ("cmd-shim under pwsh tab", {
            1000: ("wezterm-gui.exe", 1), 1001: ("pwsh.exe", 1000),
            1002: ("cmd.exe", 1001), 1003: ("uv.exe", 1002),
            1004: ("python.exe", 1003)}, 1004, 1001),
        ("cmd is the tab shell", {
            1000: ("wezterm-gui.exe", 1), 1001: ("cmd.exe", 1000),
            1002: ("uv.exe", 1001), 1003: ("python.exe", 1002)}, 1003, 1001),
        ("launcher pwsh above emulator ignored", {
            900: ("pwsh.exe", 1), 1000: ("wezterm-gui.exe", 900),
            1001: ("pwsh.exe", 1000), 1002: ("uv.exe", 1001),
            1003: ("python.exe", 1002)}, 1003, 1001),
        ("headless → 0 (disabled)", {
            500: ("services.exe", 1), 1002: ("uv.exe", 500),
            1003: ("python.exe", 1002)}, 1003, 0),
        ("cycle-safe", {1: ("a.exe", 2), 2: ("b.exe", 1)}, 1, 0),
        ("broken chain → 0", {1003: ("python.exe", 1002)}, 1003, 0),
        ("bash wrapper tab", {
            1000: ("wezterm-gui.exe", 1), 1001: ("bash.exe", 1000),
            1002: ("uv.exe", 1001), 1003: ("python.exe", 1002)}, 1003, 1001),
        ("self is shell → parent anchor", {
            1000: ("wezterm-gui.exe", 1), 1001: ("pwsh.exe", 1000),
            1003: ("pwsh.exe", 1001)}, 1003, 1001),
    )
    for name, pid_index, start_pid, want in cases:
        check(name, anchor(pid_index, start_pid), want)


def test_watchdog_poll_requires_two_conclusive_misses():
    """Unknown snapshots reset, rather than bridge, the terminal-loss streak."""
    self_pid = 1004
    live = {
        1000: ("wezterm-gui.exe", 1),
        1001: ("pwsh.exe", 1000),
        self_pid: ("python.exe", 1001),
    }
    missing = {self_pid: ("python.exe", 1)}
    missing_self = {1001: ("pwsh.exe", 1000)}
    poll = saikai._terminal_watchdog_poll

    misses = 0
    for snapshot in (None, None):                      # fail, fail
        misses, kill = poll(misses, snapshot, self_pid)
        assert (misses, kill) == (0, False)

    misses = 0
    for snapshot, want in ((missing, (1, False)),       # miss, fail, miss
                           (None, (0, False)),
                           (missing, (1, False))):
        misses, kill = poll(misses, snapshot, self_pid)
        assert (misses, kill) == want

    misses = 0
    for snapshot, want in ((missing, (1, False)),       # miss, fail, miss, miss
                           (None, (0, False)),
                           (missing, (1, False)),
                           (missing, (2, True))):
        misses, kill = poll(misses, snapshot, self_pid)
        assert (misses, kill) == want

    misses, kill = poll(1, missing_self, self_pid)
    assert (misses, kill) == (0, False)                 # missing self is unknown
    misses, kill = poll(1, live, self_pid)
    assert (misses, kill) == (0, False)                 # live anchor clears streak
    misses, kill = poll(misses, missing, self_pid)
    assert (misses, kill) == (1, False)                 # live cannot bridge misses


def test_watchdog_startup_does_not_arm_from_unknown_snapshot():
    """An unknown startup snapshot must not create an immortal polling thread."""
    import threading

    original_platform = saikai.sys.platform
    original_index = saikai._win_pid_index
    original_thread = threading.Thread
    original_disable = os.environ.pop("SAIKAI_NO_TERMINAL_WATCHDOG", None)
    started = []

    class _Thread:
        def __init__(self, *args, **kwargs):
            started.append((args, kwargs))

        def start(self):
            raise AssertionError("unknown watchdog snapshot must not arm")

    try:
        saikai.sys.platform = "win32"
        saikai._win_pid_index = lambda **_kwargs: None
        threading.Thread = _Thread
        saikai._start_terminal_watchdog(poll_sec=0)
        assert started == []
    finally:
        threading.Thread = original_thread
        saikai._win_pid_index = original_index
        saikai.sys.platform = original_platform
        if original_disable is not None:
            os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = original_disable


def test_win_pid_index_distinguishes_empty_from_failed_snapshot():
    """Snapshot failures are quiet for readers and explicit for destructive checks."""
    import ctypes

    class _Fn:
        def __init__(self, result=None, error=None):
            self.result, self.error = result, error

        def __call__(self, *args):
            if self.error:
                raise self.error
            return self.result

    class _Kernel32:
        def __init__(self, create, first, next_, last_error=18):
            self.CreateToolhelp32Snapshot = create
            self.Process32First = first
            self.Process32Next = next_
            self.GetLastError = _Fn(last_error)
            self.CloseHandle = _Fn(1)

    original = ctypes.windll
    try:
        def assert_failed(kernel):
            ctypes.windll = type("_Windll", (), {"kernel32": kernel})()
            assert saikai._win_pid_index() == {}
            try:
                saikai._win_pid_index(strict=True)
            except saikai._SnapshotFailed:
                pass
            else:
                raise AssertionError("strict snapshot failure was treated as complete")

        for kernel in (
                _Kernel32(_Fn(error=OSError("create")), _Fn(0), _Fn(0)),
                _Kernel32(_Fn(1), _Fn(error=OSError("first")), _Fn(0)),
                _Kernel32(_Fn(1), _Fn(1), _Fn(error=OSError("next"))),
                _Kernel32(_Fn(1), _Fn(0), _Fn(0)),
                _Kernel32(_Fn(1), _Fn(0), _Fn(0), last_error=5),
                _Kernel32(_Fn(1), _Fn(1), _Fn(0), last_error=5),
                _Kernel32(_Fn(1), _Fn(1), _Fn(0)),
        ):
            assert_failed(kernel)
    finally:
        ctypes.windll = original


def test_live_session_pid_keeps_unknown_windows_snapshot_alive():
    """An inconclusive PID snapshot cannot hide every live-session registry row."""
    original_platform = saikai.sys.platform
    original_alive = saikai._is_pid_alive
    try:
        saikai.sys.platform = "win32"
        saikai._is_pid_alive = lambda pid: pid == 42
        assert saikai._is_session_pid_live(42, None) is True
    finally:
        saikai._is_pid_alive = original_alive
        saikai.sys.platform = original_platform


def test_destructive_pid_check_rejects_unknown_windows_snapshot():
    """Unknown process identity must not authorize taskkill against a reused PID."""
    original_platform = saikai.sys.platform
    original_index = saikai._win_pid_index
    original_alive = saikai._is_pid_alive
    try:
        saikai.sys.platform = "win32"
        saikai._win_pid_index = lambda: None
        saikai._is_pid_alive = lambda pid: True
        assert saikai._proc_start_matches(42, "") is False
    finally:
        saikai._is_pid_alive = original_alive
        saikai._win_pid_index = original_index
        saikai.sys.platform = original_platform


def test_destructive_pid_check_requires_exact_windows_creation_ticks():
    """A same-name process at a recycled PID must not authorize taskkill."""
    original_platform = saikai.sys.platform
    original_index = saikai._win_pid_index
    original_start = getattr(saikai, "_win_process_start", None)
    try:
        saikai.sys.platform = "win32"
        saikai._win_pid_index = lambda **_kwargs: {42: ("claude.exe", 7)}
        saikai._win_process_start = lambda pid: "639198371002213410"

        assert saikai._proc_start_matches(42, "") is False
        assert saikai._proc_start_matches(
            42, "639198371002213409") is False
        assert saikai._proc_start_matches(
            42, "639198371002213410") is True

        saikai._win_process_start = lambda pid: None
        assert saikai._proc_start_matches(
            42, "639198371002213410") is False
    finally:
        if original_start is None:
            del saikai._win_process_start
        else:
            saikai._win_process_start = original_start
        saikai._win_pid_index = original_index
        saikai.sys.platform = original_platform


if __name__ == "__main__":
    test_find_terminal_anchor()
    print("PASS test_find_terminal_anchor")
    test_watchdog_poll_requires_two_conclusive_misses()
    print("PASS test_watchdog_poll_requires_two_conclusive_misses")
    test_watchdog_startup_does_not_arm_from_unknown_snapshot()
    print("PASS test_watchdog_startup_does_not_arm_from_unknown_snapshot")
    test_win_pid_index_distinguishes_empty_from_failed_snapshot()
    print("PASS test_win_pid_index_distinguishes_empty_from_failed_snapshot")
    test_live_session_pid_keeps_unknown_windows_snapshot_alive()
    print("PASS test_live_session_pid_keeps_unknown_windows_snapshot_alive")
    test_destructive_pid_check_rejects_unknown_windows_snapshot()
    print("PASS test_destructive_pid_check_rejects_unknown_windows_snapshot")
    test_destructive_pid_check_requires_exact_windows_creation_ticks()
    print("PASS test_destructive_pid_check_requires_exact_windows_creation_ticks")
    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)
