# recap TUI Redesign — Design Spec

**Date**: 2026-05-27  
**Status**: Approved

## Background

The current Textual-based TUI (`textual_pick`) has 18 keybindings and no persistent status display. Users reported the footer as overwhelming and the absence of mode indicators (session count, sort order, Tree/Cluster state) as a pain point.

## Goals

1. Reduce cognitive load by cutting visible keybindings ~18 → 8 (↑↓ navigation stays implicit in the DataTable)
2. Add a persistent status bar showing current state at a glance
3. Consolidate redundant bindings without losing any real capability
4. Keep: preview split (always-visible right pane), Tree mode, Cluster mode

## Layout

```
┌─ Search ────────────────────────────────────────────────────────────────────┐
│  Search: filter title / msg / SID  •  :fav  :hidden  :open                 │
├─ Status bar (NEW) ──────────────────────────────────────────────────────────┤
│  274 sessions  ·  Sort: Start↓  ·  All projects  ·  Tree: OFF  ·  Cluster: OFF │
├─ Table (60%) ───────────────────────────────────┬─ Preview (40%) ───────────┤
│    Start        Last   Title                    │  preview content          │
│  ──────────────────────────────────────────────  │  ─────────────────────── │
│  ▶ 2026-05-27  3m   recap TUI redesign …        │  SID / turns / content    │
│    ...                                          │                           │
├─ Footer (8 visible bindings; ↑↓ implicit in DataTable) ────────────────────┤
│  Enter Resume  Ctrl-X Hide  Ctrl-P ★  Ctrl-G Cluster  Ctrl-T Tree          │
│  Tab Preview  ? Help  Esc Quit                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Status Bar

A new `Static` widget placed between the `Input` (search) and the `DataTable`. Updated on every `_do_refresh_table()` call.

**Content format**:
```
{N} sessions  ·  Sort: {col}{dir}  ·  {scope}  ·  Tree: {ON|OFF}  ·  Cluster: {ON|OFF}
```

- `N`: count of currently visible rows (after filter, after hide/show)
- `Sort`: first active sort key, e.g. `Start↓` or `Title^`. Falls back to `default` if no sort is set.
- `scope`: `All projects` | `{repo name}` | `{path}`
- `Tree` / `Cluster`: green `ON` or dim `OFF`

## Keybinding Changes

### Removed (6 bindings)

| Binding | Old function | Replacement |
|---------|-------------|-------------|
| `Alt-1` `Alt-2` `Alt-3` | Sort priority 1/2/3 | Column header click |
| `Alt-Q` `Alt-W` `Alt-E` | Sort direction toggle | Column header click (2nd click reverses) |
| `Ctrl-R` | Show hidden sessions | Search `:hidden` token (already works) |

### Consolidated (2 → 1)

| Old | New | Function |
|-----|-----|----------|
| `Ctrl-F` (full preview) + `Ctrl-S` (summary preview) | `Tab` | Toggle preview mode full ↔ summary |

### Retained (8 shown in footer; ↑↓ implicit in DataTable)

| Key | Action |
|-----|--------|
| `↑` `↓` | Navigate rows |
| `Enter` | Resume session |
| `Esc` / `Ctrl-C` | Quit |
| `Ctrl-X` | Toggle hide/unhide current session |
| `Ctrl-P` | Toggle ★ favorite |
| `Ctrl-G` | Toggle Cluster mode |
| `Ctrl-T` | Toggle Tree mode |
| `Tab` | Toggle preview full ↔ summary |
| `?` | Open help overlay |

## Help Overlay (`?`)

A `ModalScreen` or inline `ContentSwitcher` that shows the full keybinding reference, grouped by category:

- **Navigation**: ↑↓, Enter, Esc
- **Session ops**: Ctrl-X (Hide), Ctrl-P (★ Fav), note on `:hidden` / `:fav` search
- **Display modes**: Ctrl-G (Cluster), Ctrl-T (Tree), Tab (Preview toggle)
- **Sorting**: header click (sort), 2nd click (reverse); note on multi-level sort via repeated clicks

Dismiss with `Esc` or `?` again.

## Implementation Notes

### Status bar widget
- Add a `Static` widget with `id="statusbar"` in `compose()`, between `Input` and `Horizontal`.
- In `_do_refresh_table()`, call `self.query_one("#statusbar", Static).update(status_text)` at the end.
- CSS: `#statusbar { height: 1; background: $surface; color: $warning; }` (1 row tall).

### Tab binding
- Replace `Binding("ctrl+f", "preview_full", ...)` and `Binding("ctrl+s", "preview_summary", ...)` with a single `Binding("tab", "toggle_preview", "Preview")`.
- `action_toggle_preview`: flip `self.preview_mode` between `"summary"` and `"full"`, then call `_update_preview()`.
- Note: Tab overrides Textual's default focus-cycling behavior between widgets; acceptable tradeoff since navigation is keyboard-arrow-driven.

### Remove Alt bindings
- Delete `Binding("alt+1", ...)` through `Binding("alt+e", ...)` (6 lines).
- Delete `action_cycle_sort` and `action_toggle_dir` methods (or keep them un-bound for potential future use).

### Help overlay
- Implement `HelpScreen(ModalScreen)` with a `Static` containing the keybinding reference.
- Add `Binding("question_mark", "push_screen('help')", "Help")` (no `show=False` — `?` is one of the 8 visible footer bindings).

### Ctrl-R removal
- Remove `Binding("ctrl+r", "toggle_view", ...)`.
- Keep `_toggle_view_mode()` and `action_toggle_view` in code (still useful internally); just un-bind from the UI.

## Non-Goals

- Changing the 60/40 split ratio (user prefers current)
- Changing column structure (Start, Last, Title — user confirmed Last is used)
- Removing Tree or Cluster mode
- Animated transitions
