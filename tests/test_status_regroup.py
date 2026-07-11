"""The Linux 'stopped sessions stay Running' regression (#linux-state-regroup).

On a quiet POSIX pty the busy→idle debounce gets its 2nd tick from the 1.5s
UI-thread poll, whose call_from_thread marshal silently no-ops — so the
reader-path _on_live_status (which requests an UNCONDITIONAL rebuild) never
fires for the flip. The poll's own rebuild request was deferred whenever ANY
live pane held focus, and the 2s rescan was skipped on the same condition, so
a user parked in a pane (the normal posture; ConPTY's chatty idle output hides
this on Windows) watched finished sessions sit under "Running" forever.

The deferral exists to protect TYPING, not focus: it now defers only while
keys recently went into the focused pane.

Run:  python tests/test_status_regroup.py     (needs textual + ptyprocess)
"""
import json
import os
import sys
import tempfile
import time
import uuid

os.environ.pop("SAIKAI_MIRROR", None)
from pathlib import Path

_FAKE_HOME = Path(tempfile.mkdtemp(prefix="saikai-regroup-test-"))
for _var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[_var] = str(_FAKE_HOME)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SUMMARIZE_ENABLED"] = "0"
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"
os.environ["SAIKAI_NO_BELL"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import saikai


def _write_session() -> str:
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / "-tmp-regroup-work"
    pdir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "ai-title", "aiTitle": "Regroup probe",
         "timestamp": "2026-07-12T00:00:00.000Z", "cwd": "/tmp/regroup-work"},
        {"type": "user", "timestamp": "2026-07-12T00:01:00.000Z",
         "cwd": "/tmp/regroup-work",
         "message": {"content": "probe prompt long enough to count"}},
    ]
    (pdir / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return sid


# A child that acts like claude on POSIX: busy output (braille OSC-0 title +
# 'esc to interrupt'), an idle repaint, then TOTAL SILENCE — the final flip can
# only come from the host poll, never a reader chunk.
_CHILD = (
    "printf '\\033]0;\\xe2\\xa0\\x8b working\\007'; "
    "printf 'crunching (esc to interrupt)\\n'; "
    "sleep 6; "
    "printf '\\033]0;saikai\\007\\033[2J\\033[H> all done, quiet now\\n'; "
    "sleep 300"
)


def test_finished_pane_regroups_out_of_running_while_pane_focused():
    try:
        from textual.app import App  # noqa: F401
        import ptyprocess  # noqa: F401
    except Exception:
        print("SKIP test_finished_pane_regroups_out_of_running_while_pane_focused"
              " (textual/ptyprocess unavailable)")
        return

    import asyncio
    from textual.app import App

    sid = _write_session()
    facts: dict = {"trace": []}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.6)
                self._spawn_live_pane(
                    sid, ["bash", "-c", _CHILD], "/tmp", dict(os.environ),
                    "Regroup probe")
                for _ in range(20):                     # wait for the mount worker
                    await pilot.pause(0.25)
                    if self._live is not None and self._live.has(sid):
                        break
                term = self._live.get(sid)
                assert term is not None, "pane did not open"
                term.focus()
                await pilot.pause(0.1)

                saw_running = False
                deadline = time.monotonic() + 22
                while time.monotonic() < deadline:
                    st = self._live.status(sid) if self._live.has(sid) else "-"
                    grp = (self._sid_index.get(sid) or {}).get("_state", "?")
                    focused = self._focused_terminal() is not None
                    facts["trace"].append((round(time.monotonic(), 1), st, grp, focused))
                    if grp == "Running":
                        saw_running = True
                    if saw_running and st == "idle" and grp == "Open":
                        break                            # healthy transition seen
                    await pilot.pause(0.5)
                facts["saw_running"] = saw_running
                facts["final_status"] = self._live.status(sid)
                facts["final_group"] = (self._sid_index.get(sid) or {}).get("_state")
                facts["still_pane_focused"] = self._focused_terminal() is not None
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    trace = "\n".join(map(str, facts["trace"]))
    assert facts.get("saw_running"), f"pane never classified busy/Running:\n{trace}"
    assert facts.get("final_status") == "idle", f"status stuck: {facts['final_status']}\n{trace}"
    # THE regression: with focus parked in the pane (not typing), the finished
    # session must leave the "Running" group without waiting for focus to move.
    assert facts.get("final_group") == "Open", \
        f"row still grouped {facts['final_group']!r} after idle (stale Running):\n{trace}"
    assert facts.get("still_pane_focused"), \
        f"regrouping must not steal focus from the pane:\n{trace}"


def test_typing_still_defers_the_rebuild():
    """The protection the deferral exists for must survive the fix: keys just
    sent into the focused pane keep the rebuild deferred (no mid-typing table
    churn), and the catch-up still happens once typing stops."""
    try:
        from textual.app import App  # noqa: F401
        import ptyprocess  # noqa: F401
    except Exception:
        print("SKIP test_typing_still_defers_the_rebuild (textual/ptyprocess unavailable)")
        return

    import asyncio
    from textual.app import App

    sid = _write_session()
    facts: dict = {}

    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(110, 30)) as pilot:
                await pilot.pause(0.6)
                self._spawn_live_pane(
                    sid, ["bash", "-c", _CHILD], "/tmp", dict(os.environ),
                    "Regroup probe")
                for _ in range(20):
                    await pilot.pause(0.25)
                    if self._live is not None and self._live.has(sid):
                        break
                term = self._live.get(sid)
                term.focus()
                await pilot.pause(0.3)
                # simulate active typing: keystrokes flow into the pane
                end = time.monotonic() + 6
                while time.monotonic() < end:
                    await pilot.press("a")
                    await pilot.pause(0.4)
                facts["group_while_typing"] = (
                    (self._sid_index.get(sid) or {}).get("_state"))
                facts["typing_recent"] = self._pane_typing_recently()
                # stop typing; the poll must catch up on its own cadence
                for _ in range(12):
                    await pilot.pause(0.5)
                    if ((self._sid_index.get(sid) or {}).get("_state")) == "Open":
                        break
                facts["group_after_typing"] = (
                    (self._sid_index.get(sid) or {}).get("_state"))
        asyncio.run(go())

    orig_run, App.run = App.run, fake_run
    orig_argv = sys.argv
    try:
        sys.argv = ["saikai", "--all"]
        saikai.main()
    finally:
        App.run = orig_run
        sys.argv = orig_argv

    assert facts.get("typing_recent") is True, facts
    assert facts.get("group_after_typing") == "Open", facts


if __name__ == "__main__":
    test_finished_pane_regroups_out_of_running_while_pane_focused()
    print("PASS test_finished_pane_regroups_out_of_running_while_pane_focused")
    test_typing_still_defers_the_rebuild()
    print("PASS test_typing_still_defers_the_rebuild")
    print("ALL PASS")
