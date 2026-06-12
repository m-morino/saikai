# Keyboard-first usability + Japanese docs — design

Date: 2026-06-12
Status: approved (option A + Space leader, chosen by the author)

## Goal

saikai must be fully usable and pleasant **without a mouse**, with a low
learning curve; the mouse stays a bonus, never a requirement. Ship Japanese
documentation alongside.

## Audit that motivated this

- A leader/prefix mode exists but is **opt-in and empty** — zero out-of-box
  value, high setup cost.
- Mouse-only operations: in-app sort (header click; `action_cycle_sort` /
  `action_toggle_dir` exist but are unbound), the list/pane divider (drag
  only), top-bar dropdowns (reachable only via undocumented Shift+Tab).
- F-key bindings work and stay (footer-visible, backward compatible), but
  their mnemonics are arbitrary and laptops need Fn.

## Design

1. **Leader mode on by default, leader = `Space`** (fires only while the
   session table is focused, so it can never steal keys from a claude pane or
   the search box). `[keys] leader` overrides; `"none"` / `"off"` disables.
2. **Built-in mnemonic letter map** (module constant `DEFAULT_LEADER_MAP`),
   user `[keys]` letters merge OVER it; `leader_defaults = false` starts from
   an empty map. Defaults:
   `f`=favorite `h`=hide `e`=rename(edit) `r`=refresh `d`=diff `y`=copy(yank)
   `s`=sort-cycle `o`=sort-order `g`=group `t`=tree `c`=cluster `n`=new
   `p`=restore-panes `z`=freeze `a`=attention `l`=toggle-list `x`=close-tab
   `[`=prev-tab `]`=next-tab `Space`=mark (the old direct Space; double-Space
   keeps the muscle memory).
3. **Previously unbound actions become reachable**: sort cycle / sort
   direction (leader `s` / `o`), batch mark (leader `Space`).
4. **Divider without the mouse**: `Alt+←` / `Alt+→` nudge the split ratio by
   0.04 within the existing clamp, persisted exactly like a drag. Pure helper
   `_nudge_split_ratio` for unit tests.
5. **Discoverability**: pressing the leader shows a compact hint of the whole
   map; `?` (and leader `?`) opens help, which now renders the live leader map
   dynamically; the config template documents all of it.
6. **Docs**: README gains a *Keyboard-first* passage (leader table, "mouse is
   optional" statement, Shift+Tab for the dropdowns); full Japanese
   translation in **README.ja.md**, cross-linked language switcher at the top
   of both. CHANGELOG entry under [Unreleased].

## Non-goals

- No vim-style direct letter keys (would break search-as-you-type).
- No F-key reshuffle.

## Code structure

- `_resolve_leader(keys_cfg, id_to_action)` — new pure module-level function
  returning `(leader_key, letter_map, errors)`; the PickerApp config block
  shrinks to one call. Unit-testable without textual.
- Virtual leader action ids `sort` / `order` / `mark` map to
  `cycle_sort('1')` / `toggle_dir('1')` / `toggle_mark` (no Binding objects).
- `action_grow_list` / `action_shrink_list` + `Alt+←/→` bindings with the
  same SkipAction guards as Tab/?.

## Testing

- Headless (no textual): `_resolve_leader` default/override/disable/
  `leader_defaults=false`; every `DEFAULT_LEADER_MAP` id resolves to an
  action; `_nudge_split_ratio` clamps both ends.
- Pilot (textual, runs in CI): leader Space → `f` toggles favorite;
  `Alt+→` grows the persisted ratio.
