# recap TUI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace recap's 18-keybinding footer-heavy TUI with a persistent status bar, 8 visible keybindings (↑↓ implicit), and a `?` help overlay.

**Architecture:** All changes live inside `textual_pick()` in `recap.py` — `PickerApp` is a closure class defined inside that function, so it can reference `show_project` and `repo` parameters directly. Three sequential tasks: (1) status bar, (2) bindings cleanup, (3) help overlay. No new files.

**Tech Stack:** Python, Textual ≥0.50, Rich markup

---

### Task 1: Status bar widget

Adds a one-line `Static` widget between the search input and the table, showing session count, active sort, project scope, and Tree/Cluster ON/OFF state. Replaces `self.sub_title` updates.

**Files:**
- Modify: `recap.py:1853-1857` (imports inside `textual_pick()`)
- Modify: `recap.py:1899-1905` (CSS)
- Modify: `recap.py:1909-1916` (compose)
- Modify: `recap.py:2141-2176` (`_update_subtitle`)

- [ ] **Step 1: Add `Static` to imports**

Current line 1856:
```python
from textual.widgets import DataTable, Footer, Input, RichLog
```

Replace with:
```python
from textual.widgets import DataTable, Footer, Input, RichLog, Static
```

- [ ] **Step 2: Add `#statusbar` rule to CSS**

Current CSS block (lines 1899-1905):
```python
        CSS = """
        Screen { layout: vertical; }
        #search { dock: top; height: 3; border: tall $accent; }
        #main { layout: horizontal; height: 1fr; }
        #table { width: 60%; }
        #preview { width: 40%; padding: 0 1; border-left: solid $accent; }
        """
```

Replace with:
```python
        CSS = """
        Screen { layout: vertical; }
        #search { dock: top; height: 3; border: tall $accent; }
        #statusbar { height: 1; background: $surface; color: $warning; }
        #main { layout: horizontal; height: 1fr; }
        #table { width: 60%; }
        #preview { width: 40%; padding: 0 1; border-left: solid $accent; }
        """
```

- [ ] **Step 3: Yield `Static` in compose()**

Current `compose()` (lines 1909-1916):
```python
        def compose(self) -> ComposeResult:
            yield Input(placeholder="Search title / msg / SID / proj    "
                                    "•  :fav  :hidden  :open  :active  :recent",
                        id="search")
            with Horizontal(id="main"):
                yield DataTable(cursor_type="row", zebra_stripes=True, id="table")
                yield RichLog(id="preview", wrap=True, highlight=False, markup=False)
            yield Footer()
```

Replace with:
```python
        def compose(self) -> ComposeResult:
            yield Input(placeholder="Search title / msg / SID / proj    "
                                    "•  :fav  :hidden  :open  :active  :recent",
                        id="search")
            yield Static("", id="statusbar")
            with Horizontal(id="main"):
                yield DataTable(cursor_type="row", zebra_stripes=True, id="table")
                yield RichLog(id="preview", wrap=True, highlight=False, markup=False)
            yield Footer()
```

- [ ] **Step 4: Rewrite `_update_subtitle()` to update `#statusbar`**

Replace the entire `_update_subtitle` method body (lines 2141-2176) with the following. Note: `show_project` and `repo` are closure variables from `textual_pick()`.

```python
        def _update_subtitle(self) -> None:
            table = self.query_one("#table", DataTable)
            n = table.row_count

            # Sort: show first active sort key
            _COL_LABEL = {
                "date": "Start", "last": "Last", "title": "Title",
                "proj": "Proj", "topic": "Topic", "turns": "Turns", "fav": "Fav",
            }
            sort_keys = _load_sort()
            first = next((k for k in sort_keys if k["col"] != "-"), None)
            if first:
                arrow = "↓" if first["dir"] == "desc" else "↑"
                col_display = _COL_LABEL.get(first["col"], first["col"].capitalize())
                sort_str = f"Sort: {col_display}{arrow}"
            else:
                sort_str = "Sort: default"

            # Scope: "All projects" when --all-projects, else repo name
            scope = "All projects" if show_project else (repo.name if repo else "All projects")

            # Mode toggles with Rich markup color
            tree_str = "[green]ON[/green]" if _get_tree_mode() else "[dim]OFF[/dim]"
            cluster_str = "[green]ON[/green]" if _get_cluster_mode() else "[dim]OFF[/dim]"

            sep = "  [dim]·[/dim]  "
            text = (f"  {n} sessions{sep}{sort_str}{sep}"
                    f"{scope}{sep}Tree: {tree_str}{sep}Cluster: {cluster_str}")
            self.query_one("#statusbar", Static).update(text)
```

- [ ] **Step 5: Verify manually**

Run:
```
uv run recap.py --ui textual
```

Expected: A yellow status bar row appears between the search box and the table:
```
  274 sessions  ·  Sort: Start↓  ·  <repo name>  ·  Tree: OFF  ·  Cluster: OFF
```
- Press Ctrl-G (Cluster): `Cluster: ON` turns green
- Press Ctrl-T (Tree): `Tree: ON` turns green, Cluster turns off
- Type in search: session count updates on each keystroke

- [ ] **Step 6: Commit**

```bash
git add recap.py
git commit -m "feat(tui): add persistent status bar with session count, sort, scope, and mode indicators"
```

---

### Task 2: Simplify BINDINGS + Tab preview toggle

Removes 9 bindings (6 Alt sort/dir, Ctrl-R show-hidden, Ctrl-F full-preview, Ctrl-S summary-preview) and replaces Ctrl-F/S with a single Tab toggle.

**Files:**
- Modify: `recap.py:1873-1898` (BINDINGS)
- Modify: `recap.py` (add `action_toggle_preview` after `action_preview_summary` at line 2300)

- [ ] **Step 1: Replace BINDINGS list**

Replace the entire BINDINGS block (lines 1873-1898). Note: `?` binding comes in Task 3. `priority=True` on Tab is required to override Textual's built-in focus-cycling behavior.

```python
        BINDINGS = [
            Binding("escape", "quit", "Quit"),
            Binding("ctrl+c", "quit", show=False),
            Binding("enter", "resume", "Resume", priority=True),
            Binding("ctrl+x", "toggle_hide", "Hide"),
            Binding("ctrl+p", "toggle_fav", "★"),
            Binding("ctrl+g", "toggle_cluster", "Cluster"),
            Binding("ctrl+t", "toggle_tree", "Tree"),
            Binding("tab", "toggle_preview", "Preview", priority=True),
        ]
```

- [ ] **Step 2: Add `action_toggle_preview()` after `action_preview_summary`**

After `action_preview_summary` at line 2300, insert:
```python
        def action_toggle_preview(self) -> None:
            self.preview_mode = "summary" if self.preview_mode == "full" else "full"
            self._update_preview(self._cursor_sid())
```

Keep `action_preview_full`, `action_preview_summary`, `action_toggle_view`, `action_cycle_sort`, and `action_toggle_dir` as dead code — unbound from UI but harmless to leave in place.

- [ ] **Step 3: Verify manually**

Run:
```
uv run recap.py --ui textual
```

Expected:
- Footer shows exactly 7 bindings: `Quit Esc  Resume Enter  Hide Ctrl-X  ★ Ctrl-P  Cluster Ctrl-G  Tree Ctrl-T  Preview Tab`  (↑↓ implicit; Ctrl-C hidden; Task 3 adds `?` → 8)
- Press Tab: preview panel switches between summary and full content
- Press Alt-1 or Ctrl-R: nothing happens (no longer bound)

- [ ] **Step 4: Commit**

```bash
git add recap.py
git commit -m "feat(tui): reduce keybindings 18→8, replace Ctrl-F/S with single Tab preview toggle"
```

---

### Task 3: Help overlay (`?`)

Adds a modal screen showing all keybindings grouped by category, triggered by `?`, dismissed by `?` or `Esc`.

**Files:**
- Modify: `recap.py:1853-1857` (imports — add `ModalScreen`)
- Modify: `recap.py:1871` (insert `HelpScreen` class before `PickerApp`)
- Modify: `recap.py` (BINDINGS + `action_help` in `PickerApp`)

- [ ] **Step 1: Add `ModalScreen` to imports**

Current imports (lines 1853-1857):
```python
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal
        from textual.widgets import DataTable, Footer, Input, RichLog, Static
        from rich.text import Text
```

Replace with:
```python
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal
        from textual.screen import ModalScreen
        from textual.widgets import DataTable, Footer, Input, RichLog, Static
        from rich.text import Text
```

- [ ] **Step 2: Add `HelpScreen` class before `PickerApp` (line 1871)**

Insert the following class immediately before `class PickerApp(App):`:

```python
        class HelpScreen(ModalScreen):
            CSS = """
            HelpScreen { align: center middle; }
            #help-content {
                background: $panel;
                border: solid $accent;
                padding: 1 2;
                width: 66;
                height: auto;
                max-height: 28;
            }
            """
            BINDINGS = [
                Binding("escape", "dismiss", show=False),
                Binding("question_mark", "dismiss", show=False),
            ]

            def compose(self) -> ComposeResult:
                yield Static(
                    "[bold cyan]Navigation[/bold cyan]\n"
                    "  [yellow]↑[/yellow] [yellow]↓[/yellow]         Move rows\n"
                    "  [yellow]Enter[/yellow]       Resume session\n"
                    "  [yellow]Esc[/yellow]         Quit\n\n"
                    "[bold cyan]Session ops[/bold cyan]\n"
                    "  [yellow]Ctrl-X[/yellow]      Toggle hide/unhide"
                    "  ([dim]:hidden[/dim] in search to find them)\n"
                    "  [yellow]Ctrl-P[/yellow]      Toggle ★ favorite "
                    "  ([dim]:fav[/dim] in search to filter)\n\n"
                    "[bold cyan]Display modes[/bold cyan]\n"
                    "  [yellow]Ctrl-G[/yellow]      Cluster mode\n"
                    "  [yellow]Ctrl-T[/yellow]      Tree mode\n"
                    "  [yellow]Tab[/yellow]         Preview: full ↔ summary\n\n"
                    "[bold cyan]Sort[/bold cyan]\n"
                    "  Column header click  — sort by that column\n"
                    "  Click again          — reverse direction\n\n"
                    "[dim]Press ? or Esc to close[/dim]",
                    id="help-content",
                )

```

- [ ] **Step 3: Add `?` to `PickerApp.BINDINGS` and add `action_help()`**

Append `Binding("question_mark", "help", "Help")` to the BINDINGS list (from Task 2):
```python
        BINDINGS = [
            Binding("escape", "quit", "Quit"),
            Binding("ctrl+c", "quit", show=False),
            Binding("enter", "resume", "Resume", priority=True),
            Binding("ctrl+x", "toggle_hide", "Hide"),
            Binding("ctrl+p", "toggle_fav", "★"),
            Binding("ctrl+g", "toggle_cluster", "Cluster"),
            Binding("ctrl+t", "toggle_tree", "Tree"),
            Binding("tab", "toggle_preview", "Preview", priority=True),
            Binding("question_mark", "help", "Help"),
        ]
```

Add `action_help()` to `PickerApp` (insert after `action_toggle_preview`):
```python
        def action_help(self) -> None:
            self.push_screen(HelpScreen())
```

- [ ] **Step 4: Verify manually**

Run:
```
uv run recap.py --ui textual
```

Expected:
- Footer now shows 8 bindings (↑↓ implicit; Ctrl-C hidden), with `? Help` at the end
- Press `?`: modal overlay appears with categorized keybinding reference, dim background behind it
- Press `?` again while overlay is open: overlay dismisses, returns to table
- Press `Esc` while overlay is open: overlay dismisses (does not quit the whole app)

- [ ] **Step 5: Commit**

```bash
git add recap.py
git commit -m "feat(tui): add ? help overlay with full keybinding reference grouped by category"
```
