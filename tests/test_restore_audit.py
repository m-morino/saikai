"""Restore accounting and malformed snapshot regressions; no agent is spawned."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_home = tempfile.TemporaryDirectory(prefix="saikai-restore-", ignore_cleanup_errors=True)
for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
    os.environ[key] = _home.name
os.environ.pop("SAIKAI_MIRROR", None)
os.environ.pop("SAIKAI_CONFIG", None)
os.environ["SAIKAI_SPLIT_LIVE"] = "1"
import saikai
import saikai_terminal as rt
from textual.app import App


class RestoreAudit(unittest.TestCase):
    def setUp(self):
        apps = []
        with patch.object(App, "run", lambda app: apps.append(app)), \
                patch.object(saikai, "_install_crash_logging"):
            saikai.textual_pick([], None, True)
        self.app = apps[0]
        self.app._live = SimpleNamespace(has=lambda sid: False)
        self.app._opening_sids = set()
        self.app._sid_index = {"known": {"id": "known", "cwd": _home.name}}
        self.app._remote_origin_block = lambda sid: False
        self.app._refresh_table = lambda: None
        self.messages = []
        self.app.notify = lambda message, **kw: self.messages.append(str(message))

    def test_refused_spawn_is_not_reported_as_reopened(self):
        self.app._restore_candidates = [{"id": "known"}]
        self.app._spawn_live_pane = lambda *a, **kw: False
        with patch.object(saikai, "_build_resume_invocation", return_value=([], None, {})):
            self.app.action_restore_panes()
        self.assertFalse(any("reopened 1" in m for m in self.messages), self.messages)

    def test_confirmation_is_not_reported_as_reopened(self):
        self.app._restore_candidates = [{"id": "known"}]
        self.app._sid_index["known"]["is_open"] = True
        screens = []
        self.app.push_screen = lambda *a, **kw: screens.append(a)
        self.app.action_restore_panes()
        self.assertEqual(len(screens), 1)
        self.assertFalse(any("reopened 1" in m for m in self.messages), self.messages)

    def test_invalid_cwd_does_not_abort_remaining_restores(self):
        self.app._restore_candidates = [{"id": "bad", "cwd": 123}, {"id": "known"}]
        called = []
        self.app._open_or_attach_live = lambda sid, **kw: called.append(sid) or True
        self.app.action_restore_panes()
        self.assertEqual(called, ["known"])

    def test_invalid_snapshot_container_is_ignored(self):
        self.app._restore_candidates = 123
        self.app.action_restore_panes()

    def test_accepted_launch_is_reported_as_queued(self):
        self.app._restore_candidates = [{"id": "known"}]
        self.app._spawn_live_pane = lambda *a, **kw: True
        with patch.object(saikai, "_build_resume_invocation", return_value=([], None, {})):
            self.app.action_restore_panes()
        self.assertTrue(any("queued 1" in m for m in self.messages), self.messages)

    def test_spawn_failure_banner_supports_monochrome(self):
        from textual.color import Color
        from textual.filter import Monochrome
        from textual.geometry import Size

        class Pane(rt.AgentTerminal):
            size = Size(80, 4)

        pane = Pane(["unavailable"])
        pane._spawn_error = "spawn failed"
        strip = pane.render_line(0)
        filtered = strip.apply_filter(Monochrome(), Color(0, 0, 0))
        self.assertIn("spawn failed", filtered.text)
        blank = pane.render_line(1).apply_filter(Monochrome(), Color(0, 0, 0))
        self.assertEqual(blank.text, " " * 80)


if __name__ == "__main__":
    unittest.main()
