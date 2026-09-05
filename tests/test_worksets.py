import json
import multiprocessing
import os
import tempfile
import time
import unittest
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saikai_workspace import (
    Project,
    ResumeTarget,
    StoreBusyError,
    StoreConflictError,
    StoreCorruptError,
    StoreVersionError,
    WorkspaceState,
    WorkspaceStore,
    Workset,
    WorksetEntry,
)


def _write_one(path: str, revision: int, ready, start, results) -> None:
    store = WorkspaceStore(Path(path))
    ready.set()
    start.wait()
    try:
        saved = store.update(
            revision,
            lambda state: replace(
                state,
                worksets=(Workset(f"set-{os.getpid()}", "shared", ()),),
            ),
        )
        results.put(("saved", saved.revision))
    except StoreConflictError:
        results.put(("conflict", None))


def _hold_update(path: str, revision: int, entered, seconds: float) -> None:
    store = WorkspaceStore(Path(path))

    def change(state: WorkspaceState) -> WorkspaceState:
        entered.set()
        time.sleep(seconds)
        return state

    store.update(revision, change)


def _exit_during_update(path: str, revision: int, entered) -> None:
    store = WorkspaceStore(Path(path))

    def change(_state: WorkspaceState) -> WorkspaceState:
        entered.set()
        os._exit(17)

    store.update(revision, change)


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "workspaces.json"

    def _valid_entry(self, host_id: str, **changes) -> WorksetEntry:
        values = {
            "id": "entry-a",
            "host_id": host_id,
            "cwd": str(self.root),
            "provider": "claude",
            "target": ResumeTarget("id", "session-a"),
            "title": "A",
        }
        values.update(changes)
        return WorksetEntry(**values)

    def _reject_workset(self, workset: Workset) -> None:
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        with self.assertRaises(StoreCorruptError):
            store.update(
                initial.revision,
                lambda state: replace(state, worksets=(workset,)),
            )
        self.assertEqual(store.read(), initial)

    def test_initialize_creates_complete_versioned_state_once(self):
        store = WorkspaceStore(self.path)

        initial = store.initialize()

        self.assertEqual(initial.schema_version, 1)
        self.assertEqual(initial.revision, 0)
        self.assertEqual(initial.projects, ())
        self.assertEqual(initial.worksets, ())
        self.assertEqual(len(initial.host_id), 36)
        self.assertEqual(store.initialize(), initial)
        self.assertTrue(self.path.with_suffix(".lock").exists())

    def test_read_missing_file_does_not_initialize_it(self):
        with self.assertRaises(FileNotFoundError):
            WorkspaceStore(self.path).read()
        self.assertFalse(self.path.exists())

    def test_conflicting_writer_cannot_replace_saved_set(self):
        store = WorkspaceStore(self.path)
        first = store.initialize()
        entry = WorksetEntry(
            "entry-a",
            first.host_id,
            str(self.root),
            "claude",
            ResumeTarget("id", "session-a"),
            "A",
        )
        saved = store.update(
            first.revision,
            lambda state: replace(
                state, worksets=(Workset("set-a", "個人開発", (entry,)),)
            ),
        )
        with self.assertRaises(StoreConflictError):
            store.update(first.revision, lambda state: replace(state, worksets=()))
        self.assertEqual(store.read(), saved)

    def test_callback_receives_deep_copy(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        project = Project(
            "project-a",
            initial.host_id,
            str(self.root),
            "A",
            "claude",
            {"claude": ResumeTarget("id", "session-a")},
        )
        saved = store.update(
            initial.revision,
            lambda state: replace(state, projects=(project,)),
        )

        def mutate(state: WorkspaceState) -> WorkspaceState:
            state.projects[0].targets["claude"] = ResumeTarget("new")
            return state

        updated = store.update(saved.revision, mutate)
        project.targets["claude"] = ResumeTarget("continue")

        self.assertEqual(
            store.read().projects[0].targets["claude"], ResumeTarget("new")
        )
        self.assertEqual(updated.revision, 2)

    def test_corrupt_json_is_rejected_without_replacement(self):
        original = "{not json"
        self.path.write_text(original, encoding="utf-8")
        store = WorkspaceStore(self.path)

        with self.assertRaises(StoreCorruptError):
            store.initialize()

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_unknown_schema_version_is_rejected_without_replacement(self):
        original = json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "host_id": "host",
                "projects": [],
                "worksets": [],
            }
        )
        self.path.write_text(original, encoding="utf-8")

        with self.assertRaises(StoreVersionError):
            WorkspaceStore(self.path).initialize()

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_blank_workset_name_is_rejected(self):
        self._reject_workset(Workset("set-a", " \t ", ()))

    def test_workset_name_longer_than_80_characters_is_rejected(self):
        self._reject_workset(Workset("set-a", "a" * 81, ()))

    def test_duplicate_workset_id_is_rejected(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        duplicate = (
            Workset("set-a", "one", ()),
            Workset("set-a", "two", ()),
        )
        with self.assertRaises(StoreCorruptError):
            store.update(
                initial.revision,
                lambda state: replace(state, worksets=duplicate),
            )

    def test_duplicate_trimmed_workset_name_is_rejected(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        duplicate = (
            Workset("set-a", "個人開発", ()),
            Workset("set-b", "  個人開発  ", ()),
        )
        with self.assertRaises(StoreCorruptError):
            store.update(
                initial.revision,
                lambda state: replace(state, worksets=duplicate),
            )

    def test_invalid_provider_is_rejected(self):
        initial = WorkspaceStore(self.path).initialize()
        entry = self._valid_entry(initial.host_id, provider="other")
        self._reject_workset(Workset("set-a", "one", (entry,)))

    def test_id_mode_requires_nonempty_session_id(self):
        initial = WorkspaceStore(self.path).initialize()
        entry = self._valid_entry(
            initial.host_id, target=ResumeTarget("id", "")
        )
        self._reject_workset(Workset("set-a", "one", (entry,)))

    def test_non_id_mode_rejects_session_id(self):
        initial = WorkspaceStore(self.path).initialize()
        entry = self._valid_entry(
            initial.host_id, target=ResumeTarget("continue", "session-a")
        )
        self._reject_workset(Workset("set-a", "one", (entry,)))

    def test_two_processes_from_one_revision_have_one_winner(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        context = multiprocessing.get_context("spawn")
        ready = [context.Event(), context.Event()]
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_write_one,
                args=(str(self.path), initial.revision, ready[index], start, results),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        for event in ready:
            self.assertTrue(event.wait(5))
        start.set()
        for process in processes:
            process.join(8)
            self.assertEqual(process.exitcode, 0)

        outcomes = sorted(results.get(timeout=2)[0] for _ in processes)
        self.assertEqual(outcomes, ["conflict", "saved"])
        self.assertEqual(store.read().revision, 1)

    def test_process_exit_releases_lock_for_next_writer(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        context = multiprocessing.get_context("spawn")
        entered = context.Event()
        process = context.Process(
            target=_exit_during_update,
            args=(str(self.path), initial.revision, entered),
        )
        process.start()
        self.assertTrue(entered.wait(5))
        process.join(5)
        self.assertEqual(process.exitcode, 17)

        saved = store.update(initial.revision, lambda state: state)

        self.assertEqual(saved.revision, 1)

    def test_lock_wait_times_out_after_two_seconds(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        context = multiprocessing.get_context("spawn")
        entered = context.Event()
        process = context.Process(
            target=_hold_update,
            args=(str(self.path), initial.revision, entered, 3.0),
        )
        process.start()
        self.assertTrue(entered.wait(5))
        started = time.monotonic()
        try:
            with self.assertRaises(StoreBusyError):
                store.update(initial.revision, lambda state: state)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 1.9)
            self.assertLess(elapsed, 2.8)
        finally:
            process.join(6)
            if process.is_alive():
                process.terminate()
                process.join(2)

    def test_replace_failure_preserves_previous_file(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        original = self.path.read_bytes()

        with patch("saikai_workspace.os.replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                store.update(initial.revision, lambda state: state)

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_invalid_update_preserves_noncanonical_json_bytes(self):
        store = WorkspaceStore(self.path)
        initial = store.initialize()
        original = json.dumps(json.loads(self.path.read_text()),
                              separators=(",", ":"), sort_keys=True).encode() + b"\n\n"
        self.path.write_bytes(original)
        with self.assertRaises(StoreCorruptError):
            store.update(initial.revision,
                         lambda state: replace(state, worksets=(Workset("a", " ", ()),)))
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
