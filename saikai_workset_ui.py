"""Small Textual screens for named worksets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, ListItem, ListView, Static

from saikai_workspace import Workset, WorksetEntry


class WorksetNameScreen(ModalScreen[str | None]):
    """Collect a validated workset display name."""

    CSS = """
    WorksetNameScreen { align: center middle; }
    #workset-name-box { width: 64; height: auto; padding: 1 2; border: solid $accent; background: $panel; }
    #workset-name-error { height: 1; color: $error; }
    """

    def __init__(self, existing_names: tuple[str, ...], initial: str = ""):
        super().__init__()
        self.existing_names = tuple(name.strip() for name in existing_names)
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="workset-name-box"):
            yield Label("Workset name")
            yield Input(value=self.initial, id="workset-name")
            yield Static("", id="workset-name-error")
            with Horizontal():
                yield Button("Save", id="workset-name-save", variant="primary")
                yield Button("Cancel", id="workset-name-cancel")

    def on_mount(self) -> None:
        self.query_one("#workset-name", Input).focus()

    def _submit(self) -> None:
        name = self.query_one("#workset-name", Input).value.strip()
        error = ""
        if not name or len(name) > 80 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name):
            error = "Name must be 1–80 printable characters."
        elif name in self.existing_names and name != self.initial.strip():
            error = "A workset with that name already exists."
        if error:
            self.query_one("#workset-name-error", Static).update(error)
            return
        self.dismiss(name)

    @on(Input.Submitted, "#workset-name")
    def submitted(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "workset-name-save":
            self._submit()
        elif event.button.id == "workset-name-cancel":
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class WorksetListScreen(ModalScreen[str | None]):
    """Select a set for restore or emit an explicit management command."""

    class Manage(Message):
        def __init__(self, action: str, set_id: str | None = None):
            super().__init__()
            self.action = action
            self.set_id = set_id

    CSS = """
    WorksetListScreen { align: center middle; }
    #workset-list-box { width: 95%; max-width: 78; height: 22; padding: 1; border: solid $accent; background: $panel; }
    #workset-list { height: 1fr; }
    #workset-actions { layout: grid; grid-size: 3 2; grid-columns: 1fr 1fr 1fr; height: 6; }
    #workset-actions Button { width: 1fr; }
    """

    def __init__(self, worksets: tuple[Workset, ...]):
        super().__init__()
        self.worksets = tuple(worksets)

    def compose(self) -> ComposeResult:
        with Vertical(id="workset-list-box"):
            yield Label("Named worksets — Enter previews restoration")
            yield ListView(*[
                ListItem(Label(f"{item.name}  ({len(item.entries)} panes)", markup=False), id=f"workset-row-{i}")
                for i, item in enumerate(self.worksets)
            ], id="workset-list")
            with Horizontal(id="workset-actions"):
                yield Button("Save current", id="workset-save-current")
                yield Button("Save previous", id="workset-save-previous")
                yield Button("Rename", id="workset-rename")
                yield Button("Update", id="workset-update")
                yield Button("Delete", id="workset-delete", variant="error")
                yield Button("Close", id="workset-close")

    def _selected_id(self) -> str | None:
        view = self.query_one("#workset-list", ListView)
        index = view.index
        return self.worksets[index].id if index is not None and 0 <= index < len(self.worksets) else None

    @on(ListView.Selected, "#workset-list")
    def selected(self) -> None:
        selected = self._selected_id()
        if selected:
            self.dismiss(selected)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        action = {
            "workset-save-current": "save_current",
            "workset-save-previous": "save_previous",
            "workset-rename": "rename",
            "workset-update": "update",
            "workset-delete": "delete",
        }.get(event.button.id or "")
        if action:
            self.post_message(self.Manage(action, self._selected_id()))
            self.dismiss(None)
        elif event.button.id == "workset-close":
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class WorksetConfirmScreen(ModalScreen[bool]):
    CSS = """
    WorksetConfirmScreen { align: center middle; }
    #workset-confirm-box { width: 68; height: auto; padding: 1 2; border: solid $accent; background: $panel; }
    """

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="workset-confirm-box"):
            yield Static(self.text, markup=False)
            with Horizontal():
                yield Button("Confirm", id="workset-confirm", variant="primary")
                yield Button("Cancel", id="workset-cancel")

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "workset-confirm")

    def key_escape(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True)
class RestoreRow:
    entry: WorksetEntry
    disposition: str
    reason: str
    initially_selected: bool = True


def build_restore_rows(entries: tuple[WorksetEntry, ...], host_id: str,
                       opened: frozenset[str], opening: frozenset[str],
                       capacity: int) -> tuple[RestoreRow, ...]:
    """Worker-only folder checks using a snapshot of the UI's live state."""
    result, seen = [], set()
    for entry in entries:
        sid = entry.target.session_id
        disposition, reason = "unavailable", ""
        selected = False
        if entry.host_id != host_id:
            reason = "another host"
        elif entry.provider != "claude" or entry.target.mode != "id":
            reason = "only Claude session IDs can be restored here"
        elif sid in seen:
            reason = "duplicate session"
        else:
            seen.add(sid)
            if sid in opened:
                disposition, reason = "already_open", "already open"
            elif sid in opening:
                reason = "already opening"
            else:
                try:
                    usable = Path(entry.cwd).is_absolute() and Path(entry.cwd).is_dir()
                except (OSError, ValueError):
                    usable = False
                if not usable:
                    reason = "saved folder unavailable"
                elif capacity <= 0:
                    disposition = "ready"
                    reason = "capacity: select instead of an earlier pane, or close a live pane"
                else:
                    disposition, reason = "ready", "ready (launch checks still apply)"
                    selected = True
                    capacity -= 1
        result.append(RestoreRow(entry, disposition, reason, selected))
    return tuple(result)


class RestoreWorksetScreen(ModalScreen[tuple[str, ...] | None]):
    """Review every saved entry; only available entries can be selected."""
    CSS = """
    RestoreWorksetScreen { align: center middle; }
    #restore-box { width: 95%; max-width: 90; height: 90%; padding: 1; border: solid $accent; background: $panel; }
    #restore-rows { height: 1fr; }
    #restore-rows Checkbox { width: 100%; height: auto; }
    #restore-buttons { height: 3; }
    """

    def __init__(self, rows: tuple[RestoreRow, ...]):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="restore-box"):
            yield Label("Restore saved panes")
            yield Static("Unavailable entries stay saved. Uncheck any pane to skip it.", markup=False)
            with VerticalScroll(id="restore-rows"):
                for i, row in enumerate(self.rows):
                    entry = row.entry
                    text = (f"{entry.title or entry.target.session_id} — {row.reason}\n"
                            f"{entry.cwd}  ({entry.target.session_id or entry.target.mode})")
                    yield Checkbox(Content(text), value=row.disposition == "ready" and row.initially_selected,
                                   disabled=row.disposition != "ready", id=f"restore-row-{i}")
            with Horizontal(id="restore-buttons"):
                yield Button("Restore selected", id="restore-confirm", variant="primary",
                             disabled=not any(r.disposition == "ready" for r in self.rows))
                yield Button("Cancel", id="restore-cancel")

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "restore-confirm":
            self.dismiss(tuple(row.entry.id for i, row in enumerate(self.rows)
                               if row.disposition == "ready" and
                               self.query_one(f"#restore-row-{i}", Checkbox).value))
        elif event.button.id == "restore-cancel":
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)
