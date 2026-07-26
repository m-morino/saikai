# Terminal correctness hardening design

**Status:** Approved by the repository owner on 2026-07-26.

**Goal:** Make split-live terminal behavior correct and bounded across Windows
Terminal/ConPTY, WezTerm, and POSIX PTYs, with special attention to native cursor
placement, IME candidate placement, Unicode graphemes, ordered terminal queries,
and teardown.

## Scope

This design covers every confirmed finding from the 2026-07-26 terminal audit:

- Unicode extended grapheme clusters and native cursor placement;
- fragmented CSI/OSC/DCS parsing, query ordering, DEC private modes, alternate
  screen state, synchronized output, and cursor shape;
- application-cursor and Kitty keyboard input;
- OSC 52 focus authorization and fragmented notifications;
- resize, scrollback pinning, status deadlines, and bounded color caches;
- child environment normalization for nested terminal emulators;
- non-blocking ordered PTY writes, natural EOF ownership, Windows/POSIX process
  tree teardown, tracked escalation work, and watchdog snapshot failures;
- parity between package dependencies and PEP 723 script metadata.

The audit's unconfirmed locale concern is handled as a UTF-8 contract test and
explicit child environment normalization. It does not add locale-dependent
decoding heuristics.

## Constraints

- `saikai_terminal.py` remains provider-neutral. Provider launch and status
  semantics remain in `saikai_provider.py`.
- No call to `_marshal`, `call_from_thread`, or another blocking cross-thread
  operation may occur while `AgentTerminal._lock` is held.
- Neither POSIX `ptyprocess.close()` nor Windows `pywinpty.close(force=True)` may
  run on the Textual UI thread.
- Every process-tree reap or escalation thread is registered and joined through
  a bounded shutdown path.
- PTY-driven repaint and status work remains coalesced.
- The final screen of a naturally exited process remains renderable, while the
  pane immediately relinquishes the PTY and PID so later unmount cannot target a
  recycled PID.
- Input and output buffers, timers, caches, threads, and parser carries have
  explicit bounds.

## Architecture

### 1. Stateful ordered VT tokenizer

Replace the collection of pre-scan regular expressions and `_esc_carry` with one
incremental tokenizer. It accepts decoded Unicode text and emits complete tokens
in original byte-stream order:

- printable/control text;
- CSI, including the full parameter, intermediate, and final-byte grammar;
- OSC terminated by BEL or ST;
- DCS terminated by ST (BEL remains a defensive compatibility terminator);
- simple ESC sequences.

Incomplete CSI, OSC, DCS, and ESC tokens remain in a bounded carry until the next
PTY read. A malformed or oversized string fails open as visible text rather than
black-holing the pane. C1 CSI/OSC/DCS/ST forms are normalized to the same token
types.

Each complete token is dispatched once, at its stream position:

1. presentation text and ordinary controls go through the synchronized-output
   stager;
2. DECSET/DECRST parameters update mode state one parameter at a time, including
   combined forms;
3. DA, DSR, DECRQM, XTVERSION, and color queries reply in encounter order;
4. positional DSR flushes preceding retained presentation before measuring the
   cursor, while state-only DECRQM remains answerable without exposing a partial
   frame;
5. OSC side effects and DCS suppression operate on complete strings, so chunk
   boundaries cannot change behavior.

This removes final-chunk-state DECRQM answers, reversed DA/DSR replies, duplicate
query loss, split `CSI ? 2026 $ p` hangs, and split notification loss.

### 2. Truthful terminal capability and mode model

One private-mode dispatcher owns DECCKM, cursor visibility, focus reporting,
mouse protocol, bracketed paste, synchronized output, and alternate-screen
modes. `_decrqm_report()` reads only this model. Mode 1004 is reported.

Primary DA identifies a conservative color VT-class terminal and omits features
saikai does not implement, notably sixel, downloadable character sets, macros,
and rectangular editing. XTVERSION identifies saikai rather than Windows
Terminal. DCS remains bounded and suppressed because it is unsupported; the
capability response no longer invites a sixel sender.

Kitty keyboard query/set/push/pop tokens are consumed into a bounded protocol
state and answered consistently. Input encoding receives the pane's current
DECCKM and Kitty state. Unmodified application cursor/Home/End keys use SS3;
modified keys use xterm modifier forms; negotiated Kitty CSI-u represents
otherwise ambiguous modified Enter/control/printable keys. Legacy Alt input uses
the event character when available, preserving punctuation and shifted symbols.

DECSCUSR updates a pane cursor-shape state. Native cursor synchronization applies
both screen-space position and the shape supported by the installed Textual
version, and restores Textual's default when the pane loses focus, hides its
cursor, dies, or unmounts.

### 3. Grapheme-correct presentation

Add the `regex` dependency and segment printable text using Unicode `\X`.
Grapheme presentation is incremental and bounded:

- adjacent text fragments are joined before segmentation;
- the potentially extensible trailing cluster is retained across PTY reads;
- any control token, cursor query, synchronized-frame boundary, render snapshot,
  resize, EOF, or short idle deadline commits the pending cluster;
- the idle deadline prevents a quiet child from hiding its last printable
  character.

`_SaikaiHistoryScreen` writes one extended grapheme per leading cell. Width is
computed for the cluster as rendered by Rich/wcwidth, regional-indicator pairs
remain a two-cell flag, and continuation cells are cleared deterministically.
The pyte cursor therefore advances by the same number of terminal cells that
Textual renders. Combining marks, variation selectors, emoji modifiers, ZWJ
families, flags, and keycaps work even when decoded PTY reads split them.

The native cursor anchor is always derived from the committed screen cursor. A
pending grapheme is committed before an IME anchor or CPR is published.

### 4. Real main/alternate buffers

Maintain separate main and alternate pyte screen/stream pairs. Entering 47, 1047,
or 1049 switches to a clean alternate buffer without destroying main history;
leaving restores the exact main buffer and cursor. Repeated sets/resets are
idempotent, combined DECSET forms work, and both buffers resize together. The
alternate buffer has bounded/no scrollback. Mirror presentation still receives
the original transitions in order.

### 5. Bounded presentation deadlines and caches

Synchronized output owns one generation-checked deadline. Starting a retained
frame arms it; a clean close cancels it; expiry fails the frame open even if the
child becomes quiet. The timeout path feeds the frame, bumps the screen version,
and schedules the existing coalesced UI work outside the terminal lock.

Scrollback pinning records a stable snapshot/generation rather than inferring new
history from deque length. Eviction at `maxlen` therefore cannot move a pinned
view or make copy-selection read different cells from those displayed.

Resize updates both screen buffers and the PTY, clamps scroll/cursor state, bumps
the screen version, invalidates render/status caches, and schedules native cursor
and mirror synchronization after releasing the lock.

Recent-input classification records its expiry deadline and schedules one
generation-checked invalidation, so an unchanged prompt is reclassified after
the four-second grace period. Paste and all mirror/local input paths update the
same timestamp.

Color conversion uses a bounded LRU for palette/index colors and does not retain
an unbounded dictionary of arbitrary truecolor values.

### 6. Side-effect authorization and child environment

OSC 52 writes are honored only when the pane is attached, visible, alive, and is
the app's focused widget on the active screen. Background panes may still copy a
selection through saikai's explicit pane-local copy action; child-originated OSC
52 never bypasses focus.

OSC 9, 777, and 99 accept BEL and ST and are parsed after complete OSC assembly.

Child launch environment scrubs outer-terminal identity and live IPC variables
before adding saikai's deliberate identity. Exact and prefix policies cover
WezTerm, tmux/screen, Kitty, Alacritty, Konsole, GNOME Terminal, and inherited
terminfo overrides/sockets. Locale variables are normalized to a UTF-8 locale
contract without changing user language preference.

### 7. Ordered non-blocking PTY writer

Every child write, including one-byte keystrokes and query replies, is enqueued
to one per-pane writer. The UI and reader threads never call a potentially
blocking PTY `write()` directly.

The writer uses a deque/condition, maintains UTF-8 byte accounting in O(1), and
has a byte cap. Enqueue is non-blocking and FIFO. One oversized write or a full
queue is rejected visibly/logged without reordering later accepted writes.
Teardown stops acceptance, clears queued application input, wakes the worker,
and joins it through a bounded tracked-worker path.

### 8. Exact PTY ownership and teardown

A lifecycle lock guards the `(pty, pid, generation)` ownership tuple. Explicit
kill and reader EOF atomically detach only the generation they own. Once
detached, `kill()` is a no-op for that process and cannot target a recycled PID.

On natural EOF:

- the final synchronized/grapheme carry is flushed;
- the reader detaches the PTY/PID before posting the dead event;
- Windows close and POSIX wait/close run on a tracked reap thread;
- POSIX direct children are waited/reaped, and the process group is checked and
  escalated even when the direct child has already died but a descendant keeps
  the slave PTY open.

On explicit kill, the UI thread only sets stop state, detaches ownership, wakes
the writer, and posts non-blocking signals. Windows `close(force=True)` and
`taskkill /T`, and POSIX close/wait/escalation, remain off-thread. Every helper
thread created for close or SIGKILL escalation is tracked and included in the
bounded shutdown join.

### 9. Inconclusive Windows process snapshots

`_win_pid_index()` returns `None` when snapshot creation or enumeration fails,
and a dictionary (possibly empty) only after a successful enumeration.
Watchdog polling distinguishes:

- conclusive live anchor: clear miss count;
- conclusive missing anchor: increment consecutive misses;
- inconclusive snapshot: clear the streak and take no action.

Startup does not arm without a conclusive snapshot. Live-session PID checks also
treat `None` as unknown rather than process absence.

## Dependency metadata

`pyproject.toml` adds `regex`. The PEP 723 block in `saikai.py` mirrors all
runtime dependencies, including `regex`, `segno`, and `cryptography`, so direct
script execution and installed-package execution have the same contract.

## Verification

Every change is test-first. New regression cases cover:

- split CSI intermediate, OSC/DCS, repeated/mixed queries, same-chunk mode
  transitions, combined modes, and quiet synchronized-output timeout;
- ZWJ, VS16, keycap, flag, combining, emoji-modifier, and split-read graphemes,
  with pyte cursor width equal to Rich `cell_len`;
- alternate-screen restoration, full-deque scroll pinning, resize cache/version
  and CPR clamping;
- DECCKM, Kitty negotiation/stack, modified Enter/control, Alt punctuation;
- focused/background OSC 52, split BEL/ST notifications, conservative DA;
- all-write FIFO/non-blocking/byte caps, natural EOF detachment, Windows off-UI
  close, POSIX surviving process groups, and tracked agent escalation;
- watchdog miss/failure sequences and environment scrubbing;
- bounded caches, timers, queues, and thread cleanup.

Mandatory suites are `tests/test_terminal_concurrency.py`,
`tests/test_resource_bounds.py`, `tests/test_real_pty_backend.py`, and
`tests/test_terminal_watchdog.py`. The complete direct-script suite, Python
compilation, dependency metadata validation, and an independent whole-branch
review are required before completion.
