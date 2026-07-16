# Synchronized output atomicity design

## Problem

On Windows Terminal, a focused split-live Claude pane can visibly tear, move the
IME candidate anchor to the pane's top-left, and flicker the native cursor while
Claude redraws its prompt.

The failure was reproduced on `master` at `c24dc71` with
`SAIKAI_IME_DEBUG=1` and `SAIKAI_PTY_CAPTURE` enabled. The captured run contained:

- 958 IME sync observations: 724 hidden and 234 anchored;
- 84 hidden/anchored state transitions;
- 22 anchors at the content-region origin `(41, 6)` instead of the prompt;
- raw PTY output containing 408 `?25l`, 229 `?25h`, 180 `?2026h`, and
  179 `?2026l` sequences.

The installed Windows Claude Code binary was verified before analysis:

- version `2.1.211`, native `win32-x64` binary;
- valid Authenticode signature from `Anthropic, PBC`;
- SHA-256
  `3d8509ae7de11d77dbdc711aa320fc6d5064ce795464a8670696611b57093caf`,
  matching Anthropic's `2.1.211` release manifest;
- size `253293728`, also matching the manifest.

Its bundled renderer creates one output string per frame, wraps the full patch in
DEC synchronized-output markers (`?2026h ... ?2026l`), and places cursor-hide,
cursor movement, drawing, and cursor-show actions inside that block. The official
Claude Code changelog also records synchronized output as the fix for tmux
rendering flicker.

Saikai advertises and tracks DEC mode 2026, but currently feeds every ConPTY read
chunk into pyte immediately. It only defers scheduling a Textual repaint while a
block is open. A repaint queued for frame N can therefore execute after the reader
has already fed the opening chunks of frame N+1. Textual and the IME cursor sync
then observe an intermediate screen such as `cursor hidden, cursor=(0,0)`.

This queued-repaint race is the underlying boundary failure behind both the
visible half-frame and the IME/native-cursor instability. Two existing details
amplify it: synchronization state is currently derived from only the last marker
in a ConPTY chunk, and the 200 ms repaint fail-open deliberately exposes a partial
screen. Agent `busy` classification cannot be the boundary because the same full
repaint happens while idle and while typing.

## Goals

- Make a `?2026h ... ?2026l` block atomic to pyte and every downstream consumer.
- Prevent Textual rendering, status classification, and IME synchronization from
  observing a partial synchronized frame.
- Preserve prompt cursor tracking after a completed frame.
- Keep PTY rendering provider-neutral and preserve all concurrency invariants.
- Bound memory and fail open for malformed or unterminated synchronized output.

## Non-goals

- Do not add another IME-specific debounce, two-frame gate, or busy heuristic.
- Do not change Claude-specific launch/status behavior in `saikai_terminal.py`.
- Do not alter cursor semantics for programs that do not use synchronized output.
- Do not modify POSIX teardown, process-tree reaping, or PTY close behavior.

## Considered approaches

### 1. Stage synchronized PTY output before pyte (selected)

Hold bytes from `?2026h` through the matching `?2026l`, then feed the complete
block to pyte in one reader-side operation. Plain output outside a block continues
to flow immediately.

This enforces the protocol at the earliest shared boundary. pyte, Textual,
classification, and IME anchoring all receive the same completed state. It also
avoids copying every terminal cell on every frame.

### 2. Maintain a separate committed screen snapshot

Continue feeding pyte incrementally but render and anchor from an immutable copy
taken at each close marker. This is correct but duplicates live-screen state,
touches rendering, selection, resize, scrollback, and cursor code, and copies the
full grid for every frame. It has a larger correctness and performance surface.

### 3. Smooth only the native cursor / IME anchor

Debounce `?25h/?25l` or reject `(0,0)` cursor moves. This can hide one symptom but
still allows half-drawn frames and status churn. It also mistakes legitimate
top-left cursor positions for transients. This approach is rejected.

## Design

### Synchronized-output staging

Add a small provider-neutral staging helper owned by each `AgentTerminal`.
It accepts already reassembled/scrubbed PTY text and returns zero or more ordered
feed units:

- outside a synchronized block, text is returned immediately;
- on `?2026h`, the marker and subsequent text are retained;
- while open, later ConPTY chunks append to the retained block;
- on `?2026l`, the complete block is returned as one feed unit;
- plain text before or after a block remains in order;
- repeated set markers remain inside the current block;
- a stray reset marker outside a block is passed through normally;
- several complete blocks in one ConPTY chunk are returned separately and in order.

The parser scans DEC private-mode sequences in order; it must not reduce a chunk
to its last marker. It recognizes `2026` both alone and within a combined private
mode sequence. The retained representation is `list[str]` plus a final join, not
repeated string concatenation, so the bounded worst case is linear rather than
quadratic.

The existing short escape-sequence carry remains before this helper, so a marker
split at a ConPTY read boundary is reassembled before parsing.

### Feed and repaint flow

Split the current `_consume` responsibilities into:

1. raw capture and escape reassembly/scrubbing;
2. immediate terminal side-channel handling;
3. synchronized-output staging;
4. the existing pyte feed, classification, and mirror tee for each completed feed
   unit.

`_consume` reports whether it actually fed any unit. `_read_loop` schedules a
coalesced repaint only when the screen changed. An opening/continuation chunk
therefore schedules nothing. A closing chunk feeds the entire frame and schedules
one repaint. If the next frame starts before that repaint runs, its bytes remain in
the staging buffer and cannot mutate pyte, so the queued repaint still sees the
complete previous frame.

The staging buffer, not agent status, becomes the presentation boundary.

Status classification therefore changes at most once per completed feed unit and
may lag by one frame. The existing `busy` repaint freeze remains for children that
do not use synchronized output.

### Side channels and query progress

Staging presentation must not stage transport progress. After escape reassembly
and the existing scrubs, saikai immediately processes host-owned side channels
from the ordered raw stream:

- bracketed paste, mouse, focus-reporting, and other tracked DEC private modes;
- OSC 52 clipboard writes and OSC notifications;
- static terminal queries: primary DA, DSR status (`5n`), DECRQM, XTVERSION, and
  OSC foreground/background color queries.

Those operations do not require the presented pyte cursor and therefore must not
wait for `?2026l`. This prevents a child from opening a synchronized block, asking
a capability question, waiting for the answer, and deadlocking against saikai's
staging buffer.

Cursor-position DSR (`6n` / `?6n`) is stateful. If it arrives while a synchronized
block is retained, the reader performs a controlled fail-open: it releases the
retained bytes through the normal pyte feed, answers DSR from the resulting cursor,
logs the reason, and resets staging. This preserves forward progress and an honest
cursor reply at the cost of exposing a partial frame only for that exceptional
protocol interaction.

The pane-direct mirror tee remains ordered with the pyte seed under `self._lock`.
It receives completed staged units rather than moving to the raw side, so the
existing seed-before-stream invariant is preserved. A browser xterm sees the same
complete byte order, only delayed until block close.

### Failure bounds

The staging buffer is bounded to 4 MiB of decoded text. It also records the open
time and uses the existing 200 ms synchronized-update threshold as a fail-open
deadline. A size breach releases immediately in the reader call that crosses the
limit. An age breach is evaluated when the next chunk arrives. A stateful cursor
query also releases immediately as described above. On EOF/finalization, any
retained text is released once before the final repaint.

If a child opens a block and becomes permanently quiet, saikai deliberately keeps
the last completed frame rather than displaying a known half-frame. The next byte,
EOF, size breach, or stateful query releases the buffer. Until one occurs, focus
and resize continue to present/anchor the last completed pyte state. A resize that
lands mid-block can make the retained frame use old coordinates when it eventually
closes; the child's resize-driven redraw corrects the following frame. This is an
accepted transient risk and avoids a second pyte writer or timer thread.

Timeout/overflow fail-open events are written to the existing bounded saikai log.

### Concurrency

- Only the existing PTY reader thread mutates staging state and feeds pyte.
- Pyte feed remains under `self._lock`.
- No marshal, driver write, or `app.cursor_position` assignment occurs under the
  lock.
- The reader marshals only the already-coalesced repaint after releasing the lock.
- No teardown or process-reap behavior changes.

### Diagnostics

Keep `SAIKAI_IME_DEBUG` and `SAIKAI_PTY_CAPTURE`. Extend IME trace lines only as
needed to distinguish a completed-frame anchor from a hidden cursor. Do not log
user text beyond the existing explicit PTY-capture opt-in.

## Tests

Write failing tests before production changes for these observable behaviors:

1. An opening block split across chunks does not mutate the screen or schedule a
   repaint before its reset marker.
2. A complete block is fed once and exposes only its final cursor position and
   visibility; transient `?25l`, `Home`, and intermediate text are not presented.
3. A queued repaint for a completed frame remains valid when the next synchronized
   frame has opened, because pyte still contains the prior completed frame.
4. Plain prefix/suffix text and multiple blocks preserve byte order.
5. Back-to-back `?2026l ... ?2026h` markers in one chunk commit the first frame
   while retaining the second; combined DEC private parameters are recognized.
6. A marker split at the ConPTY boundary is reassembled correctly.
7. Static terminal queries inside an open block receive an immediate response.
8. Cursor-position DSR inside an open block fail-opens once, then reports the
   resulting pyte cursor without deadlock.
9. Timeout, size-bound, and EOF paths release buffered text once and reset state.
10. Existing mirror tee seed ordering remains intact with staged units.
11. Replace `test_sync_update_defers_repaint_until_close`; it asserts the old
    scheduling-only design. Remove or repurpose `_sync_deferring`,
    `_in_sync_update`, and `_sync_started` consistently with the new staging
    contract, and update their comments.

After the focused tests pass, run every `tests/test_*.py` file exactly as CI does.
Because this changes terminal/threading behavior, the mandatory evidence includes
`tests/test_terminal_concurrency.py`, `tests/test_resource_bounds.py`, and
`tests/test_pty_backend.py`, plus Python compilation.

## Real-device acceptance

Run the corrected build in Windows Terminal with Japanese IME and both diagnostic
captures enabled. Reproduce the same idle prompt typing/conversion flow and verify:

- no visible half-frame or layout tear;
- no anchor at the content-region origin during a completed prompt frame;
- no host cursor show/hide thrashing caused by intermediate synchronized chunks;
- completed prompt anchors advance with the CJK cursor position;
- search Input focus still owns its caret without a pane/search cursor fight.

Report before/after counts from the trace. Do not claim the bug fixed from unit
tests alone.
