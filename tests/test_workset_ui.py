"""Named workset UI and PickerApp integration regressions."""
import asyncio
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_home = tempfile.TemporaryDirectory(prefix="saikai-workset-ui-", ignore_cleanup_errors=True)
for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[key] = _home.name
os.environ["SAIKAI_SPLIT_LIVE"] = "1"
os.environ["SAIKAI_NO_TERMINAL_WATCHDOG"] = "1"
os.environ.pop("SAIKAI_MIRROR", None)

import saikai
from saikai_workspace import (ResumeTarget, StoreConflictError, Workset,
                              WorksetEntry, WorkspaceStore)
from saikai_workset_ui import WorksetConfirmScreen, WorksetListScreen, WorksetNameScreen
from saikai_workset_ui import RestoreWorksetScreen, build_restore_rows
from textual.app import App


class _ScreenApp(App):
    def __init__(self, screen):
        super().__init__()
        self.screen_to_push = screen
        self.result = object()
        self.management = []

    def on_mount(self):
        self.push_screen(self.screen_to_push, self._done)

    def _done(self, result):
        self.result = result

    def on_workset_list_screen_manage(self, event):
        self.management.append((event.action, event.set_id))


class WorksetScreens(unittest.TestCase):
    def test_name_screen_rejects_duplicate_and_accepts_trimmed_name(self):
        async def go():
            app = _ScreenApp(WorksetNameScreen(("個人開発",)))
            async with app.run_test() as pilot:
                await pilot.pause()
                pending = app.result
                field = app.screen.query_one("#workset-name")
                field.value = " 個人開発 "
                await pilot.press("enter")
                self.assertIs(app.result, pending)
                self.assertIn("already", app.screen.query_one("#workset-name-error").render().plain.lower())
                field.value = " 調査の続き "
                await pilot.press("enter")
                await pilot.pause()
            self.assertEqual(app.result, "調査の続き")
        asyncio.run(go())

    def test_list_returns_selected_uuid_and_exposes_management_messages(self):
        entry = WorksetEntry("entry-a", "host", _home.name, "claude",
                             ResumeTarget("id", "session-a"), "A")
        workset = Workset("set-a", "個人開発", (entry,))
        async def go():
            app = _ScreenApp(WorksetListScreen((workset,)))
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
            self.assertEqual(app.result, "set-a")
        asyncio.run(go())

    def test_management_button_delivers_selected_uuid_at_narrow_size(self):
        entry = WorksetEntry("entry-a", "host", _home.name, "claude",
                             ResumeTarget("id", "session-a"), "[red]literal[/red]")
        workset = Workset("set-a", "[red]literal[/red]", (entry,))
        async def go():
            app = _ScreenApp(WorksetListScreen((workset,)))
            async with app.run_test(size=(60, 24)) as pilot:
                await pilot.pause()
                buttons = app.screen.query("#workset-actions Button")
                self.assertEqual(len(buttons), 6)
                self.assertTrue(all(button.region.height > 0 for button in buttons))
                await pilot.click("#workset-update")
                await pilot.pause()
            self.assertEqual(app.management, [("update", "set-a")])
        asyncio.run(go())


class PickerWorksets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="saikai-worksets-case-", ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.store = WorkspaceStore(self.root / "workspaces.json")
        self.store.initialize()
        apps = []
        with patch.object(App, "run", lambda app: apps.append(app)), \
                patch.object(saikai, "_install_crash_logging"):
            saikai.textual_pick([], None, True)
        self.app = apps[0]
        self.app._workspace_store_override = self.store
        self.app._opening_sids = set()
        self.messages = []
        self.app.notify = lambda message, **kw: self.messages.append(str(message))

    def tearDown(self):
        self.tmp.cleanup()

    def _inline_workers(self):
        self.app.run_worker = lambda work, **kw: work()
        self.app.call_from_thread = lambda callback, *a, **kw: callback(*a, **kw)

    def test_snapshot_uses_registered_terminal_order_and_private_cwd(self):
        class Term:
            def __init__(self, sid, cwd):
                self.sid, self._cwd = sid, cwd
        terms = [Term("session-b", str(self.root / "b")),
                 Term("session-a", str(self.root / "a"))]
        for term in terms:
            Path(term._cwd).mkdir()
        self.app._live = type("Live", (), {"all_terms": lambda _self: list(terms)})()
        self.app._sid_index = {
            "session-a": {"id": "session-a", "ai_title": "A"},
            "session-b": {"id": "session-b", "ai_title": "B"},
        }
        entries, skipped = self.app._snapshot_current_workset()
        self.assertEqual([e.target.session_id for e in entries], ["session-b", "session-a"])
        self.assertEqual([e.cwd for e in entries], [terms[0]._cwd, terms[1]._cwd])
        self.assertEqual(skipped, ())

    def test_action_save_workset_persists_both_live_panes_via_pilot(self):
        class Term:
            def __init__(self, sid, cwd):
                self.sid, self._cwd = sid, cwd
                self.title = sid
        terms = [Term("session-a", str(self.root / "a")),
                 Term("session-b", str(self.root / "b"))]
        for term in terms:
            Path(term._cwd).mkdir()

        async def go():
            async with self.app.run_test() as pilot:
                await pilot.pause()
                self.app._workspace_store_override = self.store
                self.app._live = type("Live", (), {"all_terms": lambda _self: list(terms)})()
                self.app._opening_sids = set()
                self.app._sid_index = {
                    "session-a": {"id": "session-a", "ai_title": "A"},
                    "session-b": {"id": "session-b", "ai_title": "B"},
                }
                self.app.action_save_workset()
                for _ in range(30):
                    await pilot.pause(0.02)
                    if isinstance(self.app.screen, WorksetNameScreen):
                        break
                self.assertIsInstance(self.app.screen, WorksetNameScreen)
                self.app.screen.query_one("#workset-name").value = "個人開発"
                await pilot.press("enter")
                for _ in range(30):
                    await pilot.pause(0.02)
                    if self.store.read().worksets:
                        break
        asyncio.run(go())
        saved = self.store.read().worksets[0]
        self.assertEqual(saved.name, "個人開発")
        self.assertEqual([e.target.session_id for e in saved.entries],
                         ["session-a", "session-b"])
        self.assertEqual([e.cwd for e in saved.entries],
                         [terms[0]._cwd, terms[1]._cwd])

    def test_save_is_explicit_and_live_exit_does_not_erode_set(self):
        state = self.store.read()
        entry = WorksetEntry("entry-a", state.host_id, str(self.root), "claude",
                             ResumeTarget("id", "session-a"), "A")
        self.app._write_new_workset(state.revision, "個人開発", (entry,))
        before = self.store.read().worksets
        self.app._live = type("Live", (), {
            "get": lambda *_: None,
            "forget": lambda *_: None,
            "pane_id": lambda _self, sid: f"tab-live-{sid}",
        })()
        self.app._opened_sids = {"session-a"}
        self.app._unread = set()
        self.app._busy_seen = set()
        self.app._quitting = False
        self.app._save_open_panes = lambda: None
        self.app._request_refresh = lambda: None
        self.app._mark_not_open = lambda sid: None
        self.app._on_live_exit("session-a")
        self.assertEqual(self.store.read().worksets, before)

    def test_invalid_previous_snapshot_is_empty_and_never_changes_legacy_file(self):
        legacy = self.root / "open-panes.json"
        legacy.write_bytes(b"original")
        with patch.object(saikai, "OPEN_PANES_FILE", legacy):
            self.app._restore_candidates = [None, {"id": 3, "cwd": str(self.root)},
                                            {"id": "ok", "cwd": 7}]
            entries, skipped = self.app._snapshot_previous_workset()
            self.assertEqual(entries, ())
            self.assertTrue(skipped)
            self.assertEqual(legacy.read_bytes(), b"original")

    def test_previous_import_previews_valid_rows_and_skipped_count(self):
        valid_dir = self.root / "valid"
        valid_dir.mkdir()
        entry = WorksetEntry("entry-a", "pending", str(valid_dir), "claude",
                             ResumeTarget("id", "session-a"), "A")
        missing = WorksetEntry("entry-b", "pending", str(self.root / "missing"), "claude",
                               ResumeTarget("id", "session-b"), "B")
        screens = []
        self._inline_workers()
        self.app.push_screen = lambda screen, callback=None: screens.append((screen, callback))
        self.app._save_workset_entries((entry, missing), ("bad row",), preview=True)
        self.assertEqual(len(screens), 1)
        self.assertIsInstance(screens[0][0], WorksetConfirmScreen)
        self.assertIn("Import 1", screens[0][0].text)
        self.assertIn("skip 2", screens[0][0].text)
        self.assertIn("session-a", screens[0][0].text)

    def test_inflight_and_empty_save_leave_existing_bytes_unchanged(self):
        before = self.store.path.read_bytes()
        self.app._opening_sids = {"opening"}
        self.app._save_workset_entries(())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_inflight_and_empty_update_leave_saved_set_unchanged(self):
        state = self.store.read()
        target = Workset("set-a", "kept", ())
        saved = self.store.update(state.revision, lambda s: replace(s, worksets=(target,)))
        self.app._workset_manage_state = saved
        self.app._live = type("Live", (), {"all_terms": lambda _self: []})()

        class Event:
            action = "update"
            set_id = "set-a"
            def stop(_self):
                pass

        before = self.store.path.read_bytes()
        self.app._opening_sids = {"opening"}
        self.app.on_workset_list_screen_manage(Event())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.app._opening_sids.clear()
        self._inline_workers()
        self.app.on_workset_list_screen_manage(Event())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.app._opening_sids.clear()
        self._inline_workers()
        self.app._save_workset_entries(())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_rename_update_delete_use_revision_cas_and_uuid(self):
        state = self.store.read()
        old = WorksetEntry("old-entry", state.host_id, str(self.root), "claude",
                           ResumeTarget("id", "session-a"), "A")
        saved = self.store.update(state.revision, lambda s: replace(
            s, worksets=(Workset("set-a", "old", (old,)),)))
        self._inline_workers()
        target = saved.worksets[0]
        self.app._mutate_workset(saved, target, "rename", name="renamed")
        renamed = self.store.read()
        self.assertEqual(renamed.worksets[0].id, "set-a")
        self.assertEqual(renamed.worksets[0].name, "renamed")
        new = WorksetEntry("new-entry", "pending", str(self.root), "claude",
                           ResumeTarget("id", "session-b"), "B")
        self.app._mutate_workset(renamed, renamed.worksets[0], "update", entries=(new,))
        updated = self.store.read()
        self.assertEqual(updated.worksets[0].id, "set-a")
        self.assertEqual(updated.worksets[0].entries[0].target.session_id, "session-b")
        self.app._mutate_workset(updated, updated.worksets[0], "delete")
        self.assertEqual(self.store.read().worksets, ())

    def test_mutation_io_error_is_caught_and_preserves_bytes(self):
        state = self.store.read()
        target = Workset("set-a", "kept", ())
        state = self.store.update(state.revision, lambda s: replace(s, worksets=(target,)))
        before = self.store.path.read_bytes()

        class ReadOnlyStore:
            path = self.store.path
            def update(_self, revision, change):
                raise PermissionError("read-only")

        self._inline_workers()
        self.app._workspace_store_override = ReadOnlyStore()
        self.app._mutate_workset(state, target, "delete")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(any("read-only" in message for message in self.messages))

    def test_stale_rename_cannot_replace_saved_bytes(self):
        state = self.store.read()
        target = Workset("set-a", "kept", ())
        saved = self.store.update(state.revision, lambda s: replace(s, worksets=(target,)))
        self.store.update(saved.revision, lambda s: s)
        before = self.store.path.read_bytes()
        self._inline_workers()
        self.app._mutate_workset(saved, target, "rename", name="lost")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(any("another window" in message for message in self.messages))

    def test_conflict_does_not_change_existing_bytes(self):
        first = self.store.read()
        self.store.update(first.revision, lambda s: replace(s, worksets=()))
        before = self.store.path.read_bytes()
        entry = WorksetEntry("entry-a", first.host_id, str(self.root), "claude",
                             ResumeTarget("id", "session-a"), "A")
        with self.assertRaises(StoreConflictError):
            self.app._write_new_workset(first.revision, "stale", (entry,))
        self.assertEqual(self.store.path.read_bytes(), before)


class WorksetRestore(unittest.TestCase):
    setUp = PickerWorksets.setUp
    tearDown = PickerWorksets.tearDown

    def entries(self, *sids):
        host = self.store.read().host_id
        return tuple(WorksetEntry(str(i), host, str(self.root), "claude",
                                  ResumeTarget("id", sid), sid)
                     for i, sid in enumerate(sids))

    async def preview(self, pilot):
        for _ in range(50):
            await pilot.pause(0.02)
            if (isinstance(self.app.screen, RestoreWorksetScreen) and
                    self.app.screen.query("#restore-confirm") and
                    self.app.screen.query_one("#restore-confirm").region.width > 0):
                await pilot.pause()
                return
        self.fail("restore preview never appeared")

    def save(self, entries):
        state = self.store.read()
        self.store.update(state.revision, lambda s: replace(
            s, worksets=(Workset("set-a", "restart", entries),)))

    async def restored(self, pilot):
        for _ in range(50):
            await pilot.pause(0.02)
            if any("workset restore:" in m for m in self.messages):
                return
        self.fail(f"restore did not finish: {self.messages}")

    def test_preview_classifies_missing_foreign_duplicate_open_and_capacity(self):
        entries = self.entries("a", "missing", "foreign", "a", "open", "opening", "full")
        entries = list(entries)
        entries[1] = replace(entries[1], cwd=str(self.root / "missing"))
        entries[2] = replace(entries[2], host_id="other-host")
        rows = build_restore_rows(tuple(entries), self.store.read().host_id,
                                  frozenset({"open"}), frozenset({"opening"}), 1)
        self.assertEqual([r.disposition for r in rows],
                         ["ready", "unavailable", "unavailable", "unavailable",
                          "already_open", "unavailable", "ready"])
        self.assertIn("duplicate", rows[3].reason)
        self.assertIn("capacity", rows[6].reason)
        self.assertFalse(rows[6].initially_selected)

    def test_capacity_one_can_select_later_entry_instead(self):
        rows = build_restore_rows(self.entries("a", "b"), self.store.read().host_id,
                                  frozenset(), frozenset(), 1)
        async def go():
            app = _ScreenApp(RestoreWorksetScreen(rows))
            async with app.run_test(size=(60, 24)) as pilot:
                await pilot.pause()
                a = app.screen.query_one("#restore-row-0")
                b = app.screen.query_one("#restore-row-1")
                self.assertFalse(b.disabled)
                self.assertTrue(a.value)
                self.assertFalse(b.value)
                await pilot.click("#restore-row-0")
                await pilot.click("#restore-row-1")
                await pilot.click("#restore-confirm")
                await pilot.pause()
            self.assertEqual(app.result, (rows[1].entry.id,))
        asyncio.run(go())

    def test_partial_restore_retains_bytes_and_saved_cwd_after_fresh_app(self):
        entries = list(self.entries("a", "missing", "full"))
        entries[1] = replace(entries[1], cwd=str(self.root / "missing"))
        self.save(tuple(entries))
        before = self.store.path.read_bytes()
        launched = []
        async def go():
            async with self.app.run_test() as pilot:
                await pilot.pause()
                self.app._workspace_store_override = WorkspaceStore(self.store.path)
                self.app._remote_origin_block = lambda sid: False
                self.app._refresh_table = lambda: None
                def spawn(sid, argv, cwd, env, title, **kw):
                    if sid == "full":
                        return False
                    launched.append((sid, cwd))
                    self.app._opening_sids.add(sid)
                    return True
                self.app._spawn_live_pane = spawn
                with patch.object(saikai, "_build_claude_invocation",
                                  side_effect=lambda args, cwd, sessions: (args, cwd, {})):
                    self.app.action_restore_workset("set-a")
                    await self.preview(pilot)
                    await pilot.click("#restore-confirm")
                    await self.restored(pilot)
                self.app._opening_sids.clear()
        asyncio.run(go())
        self.assertEqual(launched, [("a", str(self.root))])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(any("queued 1" in m and "skipped" in m for m in self.messages))

    def test_inflight_after_preview_cannot_be_started_twice(self):
        self.save(self.entries("a", "a"))
        before = self.store.path.read_bytes()
        called = []
        async def go():
            async with self.app.run_test() as pilot:
                await pilot.pause()
                self.app._open_or_attach_live = lambda *a, **k: called.append(a) or True
                self.app.action_restore_workset("set-a")
                await self.preview(pilot)
                self.app._opening_sids.add("a")
                await pilot.click("#restore-confirm")
                await self.restored(pilot)
                self.app._opening_sids.clear()
        asyncio.run(go())
        self.assertEqual(called, [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_elsewhere_confirmation_keeps_saved_cwd_without_mutating_history(self):
        self.save(self.entries("a"))
        before = self.store.path.read_bytes()
        launched = []
        wrong = str(self.root / "indexed-folder")
        async def go():
            async with self.app.run_test() as pilot:
                await pilot.pause()
                indexed = saikai._new_session_stub("a", wrong, "already running")
                self.app._sid_index["a"] = indexed
                self.app._remote_origin_block = lambda sid: False
                self.app._spawn_live_pane = lambda sid, argv, cwd, env, title, **kw: launched.append(cwd) or True
                with patch.object(saikai, "_build_claude_invocation",
                                  side_effect=lambda args, cwd, sessions: (args, cwd, {})):
                    self.app.action_restore_workset("set-a")
                    await self.preview(pilot)
                    await pilot.click("#restore-confirm")
                    await self.restored(pilot)
                    self.assertEqual(launched, [])
                    self.assertTrue(any("1 awaiting confirmation" in m for m in self.messages))
                    self.assertEqual(type(self.app.screen).__name__, "OpenElsewhereScreen")
                    await pilot.press("enter")
                    await pilot.pause()
                self.assertEqual(indexed["cwd"], wrong)
        asyncio.run(go())
        self.assertEqual(launched, [str(self.root)])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_actual_capacity_gate_rechecks_after_preview(self):
        self.save(self.entries("a"))
        before = self.store.path.read_bytes()
        import saikai_terminal
        async def go():
            async with self.app.run_test() as pilot:
                await pilot.pause()
                self.app._remote_origin_block = lambda sid: False
                with patch.object(saikai, "_build_claude_invocation",
                                  side_effect=lambda args, cwd, sessions: (args, cwd, {})), \
                        patch.object(saikai_terminal.AgentTerminal, "__init__",
                                     side_effect=AssertionError("capacity gate must prevent construction")) as terminal:
                    self.app.action_restore_workset("set-a")
                    await self.preview(pilot)
                    self.app._live.max_live = 0
                    await pilot.click("#restore-confirm")
                    await self.restored(pilot)
                    terminal.assert_not_called()
                self.assertTrue(any("backstop" in m for m in self.messages), self.messages)
        asyncio.run(go())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_unindexed_background_session_keeps_existing_refusal_gate(self):
        self.save(self.entries("a"))
        before = self.store.path.read_bytes()
        called = []
        async def go():
            async with self.app.run_test() as pilot:
                await pilot.pause()
                self.app._spawn_live_pane = lambda *a, **k: called.append(a) or True
                self.app._remote_origin_block = lambda sid: False
                with patch.object(saikai, "_load_active_sessions", return_value={"a": "busy"}), \
                        patch.object(saikai, "_active_session_kinds", return_value={"a": "bg"}):
                    self.app.action_restore_workset("set-a")
                    await self.preview(pilot)
                    await pilot.click("#restore-confirm")
                    await self.restored(pilot)
        asyncio.run(go())
        self.assertEqual(called, [])
        self.assertTrue(any("background agent" in m for m in self.messages), self.messages)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_folder_removed_after_preview_is_skipped(self):
        folder = self.root / "vanished"
        folder.mkdir()
        self.save((replace(self.entries("a")[0], cwd=str(folder)),))
        before = self.store.path.read_bytes()
        called = []
        async def go():
            async with self.app.run_test() as pilot:
                await pilot.pause()
                self.app._open_or_attach_live = lambda *a, **k: called.append(a) or True
                self.app.action_restore_workset("set-a")
                await self.preview(pilot)
                folder.rmdir()
                await pilot.click("#restore-confirm")
                await self.restored(pilot)
        asyncio.run(go())
        self.assertEqual(called, [])
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
