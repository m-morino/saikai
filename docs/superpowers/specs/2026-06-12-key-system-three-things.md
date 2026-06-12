# Key system: "learn three things" (2026-06-12)

## Problem

After the keyboard-first pass, the key system still had high learning cost:

- TWO parallel command systems (leader letters AND F-keys) — double the docs,
  and the F-keys carry no mnemonics (F6=★? F7=hide?).
- The leader was invisible: nothing on screen said Space did anything, and it
  armed ONLY from the table — focus on the Tabs bar or grip made it silently
  dead ("Space がぜんぜん効いてない").
- The Group/Sort dropdowns lived in the hidden-until-`/` search row, so the
  features they control were undiscoverable.
- With the bar made default-visible, the old "Esc closes the bar first" rule
  would have demanded Esc-Esc to quit.

## Design: three things to learn, the rest is on screen

1. **Keys you already know** — `↑↓` move, `Enter` resume, `/` or type =
   search, `Tab` preview, `?` help, `Esc` = leave the current context
   (search/dropdown → list, list → quit), `Ctrl+C` quit, `F5` refresh.
2. **`Space` = the menu** — the ONE entry point to every command.
   - Footer shows `␣ Menu` (a real Binding with key_display, so it is
     advertised, not secret).
   - on_key fast path arms from the table; the non-priority `space` Binding
     catches the key when it bubbles unconsumed from any other non-typing
     widget (Tabs, grip) — "Space did nothing" is now impossible outside an
     Input/terminal, which keep their space by design.
   - Hesitate 0.6 s → the which-key hint appears, grouped Session / View /
     Panes. Fast fingers never see a toast.
3. **`Ctrl+]`** — pane → list. The only pane-context key worth learning
   (F2/F3 tab switching is an alias; everything else belongs to claude).

F-keys stay as compatibility aliases (bindings unchanged, remappable via
`[keys]`), but the docs teach ONE system; the aliases are listed in `?` only.

## Esc contract (changed)

| Focus | Esc does |
|---|---|
| search box | back to the list (bar STAYS — it's a fixture, not chrome) |
| a dropdown | back to the list |
| live pane | (pane consumes Esc = claude interrupt; the App fallback refocuses the list) |
| the list | quit (snapshot + kill-all when panes are open) |

The bar toggle moved to `␣/` (mnemonic: slash = search), persisted in
options.json `search_bar`.

## Files

- `saikai.py`: `Binding("space", "arm_leader", "Menu", key_display="␣")`,
  `action_arm_leader`, `action_toggle_search_bar`, Esc rules in `action_quit`,
  help-screen framing line, statusbar crumb `␣ menu · ? keys`.
- Regressions: `tests/test_keyboard_leader.py` —
  `test_pilot_esc_quits_and_bar_toggle` (single-Esc quit, ␣/ toggle persists,
  Esc-from-search keeps the bar), defaults-map asserts for `/`.
