"""Small Textual screens for named worksets."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from saikai_workspace import Workset


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
            yield Label("Named worksets — Enter restores (available in the next stage)")
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
