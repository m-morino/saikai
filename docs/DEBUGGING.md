# Debugging a display problem

Rendering defects here are hard to describe and easy to misattribute: a pane is a
terminal emulator inside a TUI framework, so "the display is wrong" can start in the
child, in pyte's model, in Textual's layout, or in the host terminal. This page is the
short path from a complaint to the layer that owns it.

The invariants these tools check are in
[ARCHITECTURE.md → Rendering invariants](ARCHITECTURE.md#rendering-invariants).

## One switch: `SAIKAI_DIAG=1`

```
SAIKAI_DIAG=1 python saikai.py
```

Everything lands in `~/.cache/saikai/diag/<launch timestamp>/`:

| File | What it holds |
|---|---|
| `violations.log` | Rows whose rendered width != the pane width, **with the row's text** |
| `dump-<sid>.txt` | The pane's pyte model, written automatically at the FIRST violation |
| `frames.log` | Per second, per pane: frames, `get_style_at` probes, crop rows, rows actually drawn, rows served from the live read, ms of UI thread |
| `events.log` | Every scroll (and who moved it), focus change, freeze, resize |
| `child.<sid>.txt` | Raw per-pane PTY bytes — what the child sent |
| `out.txt` | Every byte saikai wrote to the terminal |

Leave it on while chasing something. It costs a few counters and 1–20 ms/s of
snapshotting, and a defect that recurs is then already captured — asking someone to
reproduce it again with a different variable set is the expensive part.

Narrower switches still exist: `SAIKAI_FRAME_LOG`, `SAIKAI_PTY_CAPTURE`,
`SAIKAI_OUT_CAPTURE`, `SAIKAI_IME_DEBUG`, plus two levers for isolating a suspect in one
variable — `SAIKAI_FULL_REPAINT=1` (whole-widget pane repaints, i.e. before dirty-line
repainting) and `SAIKAI_NO_SYNC=1` (skip the DEC 2026 probe).

## Reading it

**Is the pane render healthy?** `frames.log`:

- `live/s` must be **0**. Anything else means rows were served from the locked live read
  instead of the pinned frame, so a frame can splice pyte generations (invariant 3).
- `drawn/s` far below `rows/s` means dirty-line repainting is working. `drawn/s ≈ rows/s`
  means every row is being re-rendered every frame.
- `render` is the UI-thread cost. Tens of ms/s is normal; hundreds means the pane is
  re-emitting far more than it needs to.

**Did a row render too wide?** `violations.log` names the row and quotes it, and the
model is dumped alongside on the first occurrence. Empty file = invariant 1 held.

**Who moved the viewport?** `events.log` distinguishes a wheel notch from the reader's
own scroll bump (it moves `_scroll` to keep a scrolled-back pane pinned as history
grows) from a resize. A missing wheel line means the wheel never reached that widget —
with mouse reporting on, the child owns it.

## Replaying offline

`child.<sid>.txt` is a pane's own byte stream, so it can be fed back into saikai's pyte
screen without the app:

```python
import saikai_terminal as rt, pyte
scr = rt._HistoryScreenBase(cols, rows, history=2000, ratio=0.5)
pyte.Stream(scr, strict=False).feed(text)      # text = the concatenated chunks
rows_now = rt._pyte_grid_lines(scr)
```

`out.txt` is a complete VT stream (absolute CUP), so feeding *that* into a plain
`pyte.Screen` reconstructs **what the terminal displayed**, frame by frame — split on
`\x1b[?2026l`, which is where saikai presents a frame.

Together they give a repro that needs nobody at a keyboard: the model side, the
displayed side, and the invariants to check against each.

## Attributing a defect

Run the same detector on **both** sides and compare. A flip that appears in `out.txt`
*and* in the child's `child.<sid>.txt` is the child's own rendering, faithfully shown; a
flip only in `out.txt` is ours. One side alone leaves it unresolved — "there is an
oscillation" (output) or "maybe it is our render" (input).

Two more controls worth reaching for early:

- **Remove saikai from the equation.** Run the child directly in the host terminal. If
  the defect survives, it is the child's or the terminal's. That is how the ambiguous
  East-Asian-width complaint (`①` overhanging its cell) was placed outside saikai in one
  step.
- **Slow the frames down.** Some defects are timing-dependent and invisible in a fast
  harness — a scroll bug here measured 0 backwards jumps with cheap frames and 4 once
  tracing made them slower. A loaded session is the slow case.

## What these instruments cannot distinguish

Write this down before trusting a negative result. Three diagnostics in this repo were
wrong in exactly this way, and each cost a round of blind patching:

- **An output capture is blind to tearing.** It proves where cells were *placed*, never
  whether their contents came from one snapshot. Positions can be provably correct while
  the contents are stale.
- **A capture shared between panes or between runs proves nothing.** `SAIKAI_PTY_CAPTURE`
  wrote every pane to one file, and every diag file is opened in append mode; replaying
  either reconstructs a screen no pane and no run ever had. Both are split now — per pane
  and per launch — but any new capture needs the same treatment.
- **A comparison that drops position lies.** Comparing rows after `.strip()` hides a
  leading-space shift; reconstructing frames without the CUP *column* mixes partial
  updates of the same row. In terminal work the position IS the data.
- **A text reconstruction cannot see colour**, so it cannot tell whether an overlay's box
  was drawn. Compare full cell state (glyph, fg, bg, attributes) when that matters.
- **Legitimate repetition is not oscillation.** The child's spinner cycles through
  glyphs and returns to earlier ones; an A→B→A detector needs a rule for that or it
  reports dozens of false positives.

## Regression tests

`tests/test_terminal_concurrency.py` and `tests/test_flag_width.py` hold the invariant
tests (frame pinning, one lock acquisition per frame, dirty-row repaint and its traps,
wide-glyph halves); `tests/test_keyboard_leader.py` holds the viewport ones (a scroll
under constant rebuilds must move only downward). Add to them rather than starting a new
file — a defect that broke an invariant belongs next to that invariant's test.

CI runs every `tests/test_*.py`. Run the full glob before pushing; the versioned
`.githooks/pre-push` does it for you (`git config core.hooksPath .githooks`).
