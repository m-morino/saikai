# Pane dirty-line repaint — design

Date: 2026-07-29
Status: approved (design), not yet implemented

## Problem

During a "storm" — Claude running in agent mode, emitting frame updates at a very
high rate into a live pane — two symptoms appear on Windows Terminal:

- **S1 Scrolling the saikai UI becomes extremely heavy / laggy.**
- **S2 Rows in the pane shift out of place**; any redraw (scroll, repaint) heals
  them, so the pyte model is correct and this is a presentation glitch.

## Evidence

Byte-level capture of saikai → WT output during a real storm
(`SAIKAI_OUT_CAPTURE`, ~6.5 MB over ~20 s, 1215 frames):

- Frames are bracketed in DEC 2026 synchronized output, balanced 1215 / 1215.
- **All** cursor moves are absolute CUP (`ESC[r;cH`); zero relative moves, no
  DECSTBM scroll region, no IND/RI, no LF on the true bottom row. A scroll, a
  relative-move drift, or an LF-scroll therefore cannot be the row-shift
  mechanism — all three are refuted by the data.
- Frames are 10–22 KB each at roughly 60 fps (hundreds of KB/s). That size
  matches re-emitting **every cell of the pane** (~46 × 40 cells with SGR
  colour runs), not a damage-based partial update.

Code facts:

- `saikai_terminal.py:1308-1310` documents the design: *"a full `refresh()` per
  read chunk … dirty-line optimisation can come later."* pyte's `dirty` set is
  maintained (`_SaikaiHistoryScreen.draw` adds to it) but **never read** by the
  render path.
- Textual chunks driver writes at 8192 bytes (`app.py:3873`), and emits
  SYNC_START / SYNC_END as their own writes (`app.py:4590`, `4594`), so one
  10–22 KB frame costs 4–5 write-queue slots.
- `textual/drivers/_writer_thread.py:9,17,26` — `Queue(MAX_QUEUED_WRITES=30)`
  and `put()` with no timeout, i.e. it **blocks** the caller when full. The
  caller is the asyncio event loop (`app.py:3821 _display` ←
  `screen.py:1224`), so a saturated queue freezes input and timer processing.
- `textual/driver.py:142` — `Driver.flush()` has an empty body and neither the
  Windows nor the Linux driver overrides it (only `linux_inline_driver`,
  `web_driver`). The per-frame `self._driver.flush()` at `app.py:3887` is
  therefore dead code, and the only flush is `if qsize() == 0: flush()`
  (`_writer_thread.py:61-62`), which **starves** under sustained load.

## Root cause

**One root: the pane re-emits its whole grid on every repaint.**

1. Per read chunk the pane requests a full-widget refresh, so Textual re-renders
   every visible row → 10–22 KB per frame.
2. At ~60 fps that saturates the 30-slot write queue; `put()` then blocks the
   asyncio event loop → **S1**.
3. A long mid-frame stall holds WT inside an open synchronized-output window
   past its sync timeout, so WT presents a half-applied screen — rows from two
   frames — healed by the next completed repaint → **S2**.

DEC 2026 is layout-transparent: an identical byte stream renders to identical
cells whether or not it is bracketed. `?2026` (enabled in `c756a9c`) is
therefore an **amplifier**, not the cause: it converts a transient tear into a
stable, redraw-healable wrong state. Disabling it would be a symptom fix and
would give back the idle hover flicker it was added to cure, so it stays.

## Design

### 1. Dirty-line region refresh (the fix)

In `saikai_terminal.py::AgentTerminal._do_pane_refresh` replace the
whole-widget `self.refresh()` with a refresh of only the rows pyte marked
dirty.

- `render_line(y)` maps widget row `y` to pyte row `y` directly while
  `self._scroll == 0` (live view), so a dirty pyte line `y` becomes
  `Region(0, y, width, 1)`.
- Snapshot **and clear** `screen.dirty` under `self._lock`, then release the
  lock before calling `refresh(*regions)` — never marshal while holding
  `self._lock` (see `docs/ARCHITECTURE.md`). Clearing is safe: nothing else in
  saikai reads `screen.dirty` (the only references are the two `dirty.add`
  calls inside `_SaikaiHistoryScreen.draw`; `_pyte_grid_lines`, the mirror and
  `snapshot_text` all read the buffer directly).
- No lost updates: `_refresh_pending` is cleared at the top of
  `_do_pane_refresh`, so a chunk arriving mid-repaint queues the next refresh.

### 2. Three correctness traps, handled explicitly

- **A cursor move does not dirty anything.** pyte adds `dirty` in `draw`, but
  cursor-positioning alone does not. `render_line` paints saikai's own reversed
  cursor cell, so a move would leave a ghost cursor on the old row. Store the
  cursor row used by the previous `_do_pane_refresh` and always add **that row
  plus the current cursor row** to the refresh set, then update the stored row.
- **Scrollback** (`self._scroll != 0`): visible rows come from history and no
  longer correspond to live pyte rows. Fall back to a full refresh — rare, and
  correctness first.
- **Dirty saturation**: a child scroll dirties every line, and passing many
  regions costs more than one full refresh. If the dirty count exceeds 60 % of
  the pane's rows, do a full refresh.

State-transition repaints (`_fail`, `_finalize`, freeze/unfreeze, resize) keep
their existing full `refresh()` and are not touched.

### 3. Deliberately out of scope (YAGNI)

Not implemented now; each is gated on measurement:

- a pane frame-rate cap during storms,
- a local `WriterThread` queue shim (larger queue, or discard superseded
  frames),
- new instrumentation — the existing `SAIKAI_OUT_CAPTURE` is enough to measure.

### 4. Exit criterion — is Textual still the right framework?

Measured after the fix, on device, reproducing a real storm:

- Pane output **≤ 50 KB/s** and scrolling feels responsive → stay on Textual;
  this ships as 0.6.0.
- Still saturating the queue and freezing the UI → the ceiling is Textual's
  per-frame compositor cost, and a pane that bypasses the compositor gets its
  own design.

Rationale for staying by default: the framework friction (IME anchor, cursor
visibility, terminal-identity spoofing, DCS scrubbing, hover repaint) is
**localised to the pane**, while the session list, search, dropdowns, modals,
footer, key handling and the web mirror are what Textual is good at. Migration
targets are unconvincing (Rich alone = reimplementing Textual; prompt_toolkit
also owns the screen; ratatui abandons Python and pyte), and the measured root
is our own unimplemented TODO, so a migration would carry the same symptom.

### 5. Upstream (report, do not PR)

One diplomatic, fact-based issue on Textualize/textual stating two findings,
with saikai's local shims kept until it lands and annotated with the issue
number so they can be removed:

- `WindowsDriver` never probes `?2026` while `LinuxDriver` does in
  `start_application_mode` — a platform asymmetry; WT answers `?2026;2$y`
  (measured). saikai currently sends the probe itself in `App.on_mount`.
- `WriterThread` flushes only when the queue momentarily empties, so flush
  starves under sustained load, and `put()` blocks the asyncio event loop
  (`Queue(30)`, no timeout).

Verified against `main` on 2026-07-29: both still present.

### 6. Tests

Headless regression tests in `tests/` (CI runs every `tests/test_*.py`):

- a single changed line refreshes only that row's region,
- a cursor-only move repaints both the old and the new cursor row (ghost-cursor
  regression),
- scrollback falls back to a full refresh,
- the existing 21 test files still pass, `test_terminal_concurrency.py`
  included.

## Notes

`SAIKAI_NO_SYNC` was added during this investigation as an A/B lever that skips
the `?2026` probe. It is kept as a documented kill switch for terminals that
misbehave with synchronized output; it is not part of the fix.
