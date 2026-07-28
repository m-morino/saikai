# Storm: pane frame tearing and per-frame relayout — design

Date: 2026-07-29 (revised the same day after an expert panel refuted the first
root-cause analysis; see "Refuted hypotheses")
Status: approved (design), not yet implemented

## Problem

During a "storm" — Claude running in agent mode, streaming frame updates at a
high rate into a live pane — two symptoms appear on Windows Terminal:

- **S1 Scrolling the saikai UI becomes extremely heavy / laggy.**
- **S2 Rows in the pane shift out of place**; any redraw heals them, so the pyte
  model is correct and only the presented image is wrong.

They have **two different roots**. The first analysis assumed one root and got
both wrong; the corrected version is below.

## Root cause S2 — the frame is torn across pyte generations

`AgentTerminal.render_line` takes `self._lock` **once per row**
(`saikai_terminal.py:1552`), and Textual calls it in a bare per-row loop with
the lock released between rows (`textual/_styles_cache.py:218-232`). The reader
thread applies a whole presentation unit **atomically** under that same lock
(`saikai_terminal.py:2785`, `self._stream.feed(...)`, with `_scr_ver += 1`
inside the hold).

So the reader can install a complete new child frame *between two `render_line`
calls*, and one composited image ends up carrying rows from two or more pyte
generations. With Claude's absolute-CUP fullscreen renderer, a mix of
generations reads exactly as **displaced rows that the next repaint heals**.
Measured by the panel: **40 of 40 render passes were torn.**

This also explains why the byte-level capture found no mechanism: every cursor
move is absolute CUP, there is no scroll region, no relative move and no
bottom-row LF, because nothing in the byte stream *moves* anything. The
positions are right; the **contents are stale**. Tearing does not appear in an
output capture at all.

The fix pattern already exists in this file: `_snapshot_frozen`
(`saikai_terminal.py:1648`) pins the visible rows as fixed lists of immutable
pyte `Char`s under a single lock hold, precisely because reading live "would
render/copy text that scrolled in". The live path never got the same treatment.

## Root cause S1 — per-frame full-screen invalidation and compositor cost

The write path is **not** the main culprit. Measured in a real Windows
Terminal at Textual's 60 fps display cap, the 30-slot blocking write queue
stalls the event loop for only 0–3.7 ms per 2000 ms (< 0.2 %), and it can
neither reorder nor drop bytes. It only becomes crippling if `_display` is
driven *faster* than the display timer.

Two things in saikai do exactly that, or make each frame far more expensive:

1. **The mirror's overflow recovery is a self-sustaining full-screen repaint
   loop.** `saikai.py:6167` is the **only** `refresh(layout=True)` in the
   repository, and it is registered as the mirror hub's repaint request. The
   hub calls it from `_drain_overflow_recovery` (`saikai_mirror.py:731-744`)
   every time `_ingest_overflow` is set — and during a storm the ingest queue
   overflows continuously, so a full **relayout + full repaint** runs per drain
   cycle. This matters directly here: the reported storms all ran with
   `SAIKAI_MIRROR=1`.
2. **The statusbar forces a layout pass on every update.**
   `saikai.py:6919` calls `Static.update(text)`, whose signature is
   `update(content, *, layout: bool = True)` and which ends in
   `self.refresh(layout=layout)` (`textual/widgets/_static.py:85,95`). The
   widget is `#statusbar { height: 1 }` — a fixed height — so the layout pass is
   pure waste.

On top of that, a "partial" update is not as partial as it looks:
`render_partial_update` crops to the **union** of all dirty regions
(`textual/_compositor.py:1179`) and re-renders every widget overlapping that
crop at full height (`_compositor.py:1053-1082`). So one dirty pane region plus
one dirty region on the other side of the split escalates into a de-facto
whole-screen render. Measured at 120×40: list scroll alone → 11 widgets, 9.6 KB,
8–14 ms; scroll + dirty pane → 21 widgets, 11.4 KB, 12–18 ms; add a statusbar
update → a screen-wide reflow every frame, ~14 KB and 17–21 ms of UI-thread
compositor time per scroll notch. That is already over a 60 Hz budget.

Shrinking what the pane declares dirty therefore shrinks the crop union, the
widget set re-rendered, and the bytes emitted.

## Refuted hypotheses (do not re-litigate)

- **A scroll, a relative-move drift, or a bottom-row LF shifts the rows.**
  Refuted by the byte capture: absolute CUP only, no DECSTBM, no IND/RI, zero
  LF on the true bottom row.
- **`?2026` synchronized output causes S2.** DEC 2026 is layout-transparent —
  an identical byte stream renders to identical cells bracketed or not. It was
  enabled in `c756a9c` to cure the idle hover flicker and it stays. Its timing
  correlation with S2 was a red herring; the tear is upstream of the terminal.
- **The `WriterThread` queue blocking the event loop is the root of S1.**
  Measured < 0.2 % stall at the 60 fps cap. It is a consequence of frame size,
  not a cause.
- **Every frame is a full-screen repaint.** Misdiagnosis: every update is a
  `ChopsUpdate`; the escalation is the crop-union described above.
- **A stranded `?2026l` leaves WT in an open sync window.** The flush story is
  real (`Driver.flush()` is an empty body, no Windows or Linux override, so the
  per-frame `app.py:3887` flush is dead code and `_writer_thread.py:61-62` only
  flushes when the queue momentarily empties) but byte order is preserved, so it
  cannot corrupt content. Reported upstream, not used as an explanation here.

## Design

Four changes, in priority order.

### 1. One grid snapshot per frame (fixes S2)

Give the live path the same guarantee `_snapshot_frozen` already gives the
frozen path: a rendered frame reads exactly one pyte generation.

- Add a per-frame snapshot of the visible rows (lists of immutable pyte
  `Char`s, as `_snapshot_frozen` builds).
- `render_line` reads from the snapshot and **does not take `self._lock`**.
- `_ensure_frame_snapshot()` builds it lazily: if there is no snapshot, take
  `self._lock` **once** and capture every visible row. The first `render_line`
  of a frame therefore pays one lock acquisition and all later rows are free.
- `_do_pane_refresh` invalidates the snapshot (`None`) before requesting the
  refresh; a `_scroll` change, freeze/unfreeze and resize also invalidate.
- Building it lazily inside `render_line` matters: Textual also renders rows we
  did not ask for (styles-cache misses, its own repaints), and those must not
  fall back to per-row live reads.
- If a stale snapshot is served for a row outside our refresh (cache miss), the
  row is *consistent with the rest of that frame* and self-heals on the next
  one. Consistency is the property we want; freshness is already handled by the
  next repaint.
- Cost: one snapshot of `lines × columns` object references (~1.8 k for a
  46 × 40 pane), rebuilt at most once per frame.

### 2. Stop the mirror's full-screen repaint loop (S1)

In the mirror overflow recovery path:

- **Drop `layout=True`.** Reseeding the mirror's pyte needs a full *repaint*,
  not a relayout.
- **Dedupe** — at most one repaint request per 500 ms, however many overflows
  occur in that window.
- **Gate on viewers** — no connected mirror client, no repaint request. The hub
  already tracks client count (`_mirror_clients_changed`).

### 3. Statusbar: no layout pass (S1, one line)

`saikai.py:6919` → `update(text, layout=False)`. The widget's height is fixed
by CSS, so nothing can reflow.

### 4. Dirty-line region refresh (S1, shrinks the crop union)

`_do_pane_refresh` refreshes only the rows pyte marked in `screen.dirty`
instead of the whole widget. Note this is **not** the S2 fix — change 1 is —
but it cuts the crop union, the widget set re-rendered, and the byte volume.

`render_line(y)` maps widget row `y` to pyte row `y` while `self._scroll == 0`,
so a dirty pyte row becomes `Region(0, y, width, 1)`. Nothing else in saikai
reads `screen.dirty` (the only references are the two `dirty.add` calls in
`_SaikaiHistoryScreen.draw`; `_pyte_grid_lines`, the mirror and `snapshot_text`
read the buffer directly), so draining it is safe.

#### Correctness traps, all handled explicitly

Verified against the installed pyte source. A repaint pyte does not ask for
becomes a **persistently stale row**, not a one-frame tear, because Textual
serves unrefreshed rows from its per-line strip cache
(`textual/_styles_cache.py:54-58`).

- **Cursor-only moves dirty nothing.** pyte adds `dirty` in `draw`; every
  cursor-positioning op (`cursor_position`, `cursor_up/down/forward/back`,
  `carriage_return`, `tab`, `backspace`, `save/restore_cursor`) does not.
  `render_line` paints saikai's own reversed cursor cell, so a move would leave
  a ghost. Always add **the previous and the current cursor row**,
  unconditionally — which also covers `?25l`/`?25h`, since DECTCEM changes
  `cursor.hidden` with no dirty at all.
- **Use `render_line`'s clamp for the cursor row.** `render_line` paints at
  `max(0, min(screen.cursor.y, screen.lines - 1))` (`saikai_terminal.py:1558`)
  because pyte does not clamp the cursor on shrink (`resize` never touches
  `cursor.y`). Recording the raw `cursor.y` after a height shrink would refresh
  a row that does not exist while the cursor is painted on `lines - 1` — a
  permanent ghost. Use the identical expression on both sides.
- **`resize` leaves out-of-range indices in `dirty`.** Its shrink path calls
  `delete_lines`, which does `dirty.update(range(cursor.y, self.lines))` while
  `self.lines` is still the old, larger value, and nothing ever prunes the set.
  Clamp every row to `0 <= y < min(screen.lines, self.size.height)` before
  making a `Region`; do not rely on the saturation fallback to hide it.
- **Own the pending set.** Drain and clear `screen.dirty` under `self._lock`,
  union it into a saikai-owned pending-rows set, build regions from that, and
  clear the pending set only after `refresh(*regions)` returns. Every other
  `refresh()` in this file is wrapped in `try/except`, so an exception must not
  swallow a repaint. Never cache the `screen.dirty` object across the lock
  release — pyte's pagination *rebinds* the attribute rather than mutating it.
- **Dirty `cursor.y - 1` on the zero-width merge.** saikai's own `draw`
  override writes `buffer[cursor.y - 1][columns - 1]` when a width-0 char (ZWJ,
  VS16, combining mark) arrives at `cursor.x == 0` (`saikai_terminal.py:385-391`)
  but only adds `dirty` for `cursor.y`. Close it at the source.
- **Dirty saturation.** A child scroll at the bottom row dirties every line
  (pyte `index()` does `dirty.update(range(self.lines))`), so a scrolling child
  legitimately gets a full refresh. If the dirty count exceeds 60 % of the
  pane's rows, refresh the whole widget instead of passing many regions.

State-transition repaints (`_fail`, `_finalize`, freeze/unfreeze, resize) keep
their existing full `refresh()`.

### Deliberately out of scope (YAGNI)

Each is gated on measurement, not assumed: a pane frame-rate cap, a local
`WriterThread` queue shim, memoizing `_cell_style`, and any new instrumentation
(`SAIKAI_OUT_CAPTURE` already answers the question).

## Measurement and the framework question

Reproduce a storm **with Claude's fullscreen renderer** — a scrolling,
chat-style child dirties every line via `index()`, so change 4 would look
ineffective for the wrong reason.

1. **Isolate S1's main driver first**: run the same storm with and without
   `SAIKAI_MIRROR`. If mirror-off is dramatically lighter, change 2 is the
   primary S1 fix, as predicted.
2. After the changes: pane output **≤ 50 KB/s** and responsive scrolling → stay
   on Textual and ship this as 0.6.0.
3. Still saturating and freezing → the ceiling is Textual's per-frame
   compositor cost, and a pane that bypasses the compositor gets its own
   design.

Staying on Textual is the default because the friction (IME anchor, cursor
visibility, terminal-identity spoofing, DCS scrubbing, hover repaint) is
**localised to the pane**, while the session list, search, dropdowns, modals,
footer, key handling and the web mirror are what Textual is good at. Migration
targets are unconvincing (Rich alone = reimplementing Textual; prompt_toolkit
also owns the screen; ratatui abandons Python and pyte), and both measured roots
are in saikai's own code, so a migration would carry the same symptoms.

## Upstream (report, do not PR)

One diplomatic, fact-based issue on Textualize/textual, with saikai's local
shims kept until it lands and annotated with the issue number. Verified against
`main` on 2026-07-29; neither is the cause of S1 or S2.

- `WindowsDriver` never probes `?2026` while `LinuxDriver` does in
  `start_application_mode` — a platform asymmetry; WT answers `?2026;2$y`
  (measured). saikai sends the probe itself in `App.on_mount`.
- `WriterThread` flushes only when its queue momentarily empties, so flush
  starves under sustained load, and `put()` blocks the asyncio event loop
  (`Queue(30)`, no timeout). `Driver.flush()` is an empty body that neither the
  Windows nor the Linux driver overrides, so the per-frame `app.py:3887` flush
  is dead code.

## Tests

Headless regression tests in `tests/` (CI runs every `tests/test_*.py`):

- **Tearing**: with a reader feeding a new generation between rows, one render
  pass yields rows from exactly one generation (the S2 regression).
- **One lock acquisition per frame**: rendering N rows acquires the pane lock
  once, not N times.
- A single changed line refreshes only that row's region.
- A cursor-only move repaints both the old and the new cursor row.
- **After a height shrink**, a cursor-only move still refreshes the *clamped*
  cursor row (the trap the obvious four tests would all pass with).
- Scrollback falls back to a full refresh.
- Mirror overflow recovery: repeated overflows produce at most one repaint
  request per window, none with no viewer connected, and never with
  `layout=True`.
- The existing 21 test files still pass, `test_terminal_concurrency.py`
  included.

## Notes

`SAIKAI_NO_SYNC` was added during the investigation as an A/B lever that skips
the `?2026` probe. It is kept as a documented kill switch for terminals that
misbehave with synchronized output; it is not part of any fix here.
