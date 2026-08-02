# Architecture

saikai is a local-first session index and terminal host for Claude Code. It
reads Claude's existing transcript history and keeps its own preferences as
small overlays. It does not rewrite the canonical transcripts or require a
daemon or database.

## Runtime modules

- `saikai.py` owns history discovery, transcript parsing, saikai-side overlays,
  sorting/filtering/grouping, the Textual application, configuration, and CLI.
- `saikai_terminal.py` owns provider-neutral PTY spawn, rendering, input,
  resize, status delivery, clipboard behavior, and teardown.
- `saikai_provider.py` owns agent-specific capabilities and invocation
  contracts. Claude Code is the integrated provider; other providers stay
  unavailable until their discovery and live-state contracts are complete.

Keep the boundary one-way: application policy may use the provider and terminal
layers, but terminal code must not import application policy.

## History model

Claude transcript JSONL files are the source of truth. saikai discovers them,
parses their useful metadata, and resumes a session from the cwd where it
started. It may maintain overlays such as favorites, hidden sessions, custom
titles, open-pane snapshots, and cached summaries under its own cache/config
directories.

Important rules:

- Never modify a Claude transcript to express a saikai preference.
- Last activity is `max(transcript mtime, last timestamped record)`. Claude can
  append useful untimed metadata, so using only the last JSONL timestamp makes a
  freshly changed session appear old.
- Convert UTC transcript timestamps to local time before date grouping or age
  comparisons.
- Optional summary generation is opt-in. Core discovery and resume must work
  without an LLM call.

## Split-live lifecycle

Each split-live pane starts a real provider process in a PTY. A background
reader thread feeds `pyte` while Textual's UI thread renders the same screen.
The lifecycle is:

1. The provider builds a launch contract.
2. `AgentTerminal` scrubs outer-terminal IPC/identity variables, normalizes the
   UTF-8 child contract, spawns the PTY, and attaches one
   `(pty, pid, generation)` ownership tuple.
3. One reader decodes PTY output and feeds the ordered terminal pipeline.
4. Presentation updates the active screen under `self._lock`; UI
   refresh/status/IME work is coalesced and marshalled after releasing it.
5. Natural EOF flushes bounded parser and synchronized-output tails, detaches
   ownership before announcing death, and reaps exactly once.
6. Explicit teardown atomically competes for the same ownership generation,
   signals as appropriate, and performs every blocking close/reap off the UI
   thread.

Windows uses ConPTY through `pywinpty`. POSIX platforms use `ptyprocess`.

### Terminal data path

Decoded output passes through one bounded incremental VT tokenizer. It retains
only incomplete control strings, fails malformed/oversize input open as literal
data, and dispatches mode changes, queries, notifications, clipboard requests,
and presentation in original stream order. APC, PM, SOS, DCS, OSC, CSI, C1
forms, cancellation, and synchronized-output boundaries therefore cannot leak
embedded controls or reorder replies.

Presentation uses extended grapheme clusters (`regex` `\X`) and Rich's cell
width policy. A complete cluster occupies one leader cell plus as many
deterministic continuation cells as Rich reports (the width is not assumed to
stop at two), so pyte cursor position, native cursor anchoring, CPR, selection,
and the browser mirror agree for combining text, variation selectors, emoji
modifiers, ZWJ sequences, keycaps, regional-indicator flags, and wider
conjuncts. MAIN and ALT are separate screen/stream pairs; switching buffers
does not destroy MAIN history or cursor state.

Every child input source—keys, paste, mouse, focus, terminal replies, and mirror
control—enqueues into one persistent per-pane FIFO writer. Its capacity is
accounted in encoded UTF-8 bytes in O(1); callers never block in `pty.write()`.

The browser mirror receives a detailed screen/state seed and installs a
grapheme provider over xterm.js using the exact Unicode width table loaded by
the running Rich renderer. When one Rich EGC is wider than xterm's two-column
cell representation, the provider splits only its xterm storage while
preserving text and total columns. Pane input returns through the same public
FIFO writer as local input.

Terminal retention is deliberately bounded: tokenizer carries and control
strings, synchronized-output bytes and age, Kitty keyboard stack, PTY write
queue, history, mirror queues, truecolor cache, status timers, and tracked
worker registries all have explicit caps or bounded shutdown.

## Concurrency invariants

Violating these rules can hard-freeze the UI or orphan agent worker processes:

1. Never call `call_from_thread`, `self._marshal`, or another blocking
   cross-thread operation while holding `self._lock`. Compute under the lock;
   marshal after releasing it.
2. Never join the reader thread from Textual's UI thread.
3. Never call `pywinpty.close(force=True)`, `ptyprocess.close()`, or
   `terminate()` on the UI thread.
   A reader can hold `io.BufferedRWPair`'s read lock while blocking in `read1()`;
   closing from the UI thread then waits on the same lock. Signal the child
   first and perform the blocking close on the reap thread.
4. Guard `(pty, pid, generation)` together. Natural EOF and explicit kill may
   detach only the generation they observed; never signal a PID from a stale
   reader callback.
5. Route every PTY write through the bounded per-pane FIFO worker. No UI,
   reader, timer, or mirror callback may call the backend writer inline.
6. Track every process-tree reap, close helper, writer, and agent escalation;
   bounded-join outstanding workers at process exit. Otherwise saikai can exit
   before descendants are terminated.
7. Coalesce PTY-driven repaint and status work. A streaming agent can emit many
   chunks and status transitions per second.

Do not add locking to fix a cosmetic race unless the lock ordering has been
proved and covered by a regression test.

## Rendering invariants

A pane is a terminal emulator inside a TUI framework, which means three width and
state models have to agree: pyte's (the model), Rich/Textual's (the layout), and the
host terminal's. Every rendering defect found so far was one of these six invariants
breaking, and each broke silently — the symptom appeared somewhere else.

1. **A cell renders in exactly the columns it owns.** pyte pairs a double-width glyph
   with an empty stub; a cell that renders 2 columns while owning 1 pushes the rest of
   the row right and spills its tail past the pane. *Symptom: a row looks shifted, and
   heals when the child redraws it without the wide glyph.* The zero-width merge has to
   claim the second column, and a 2-column cell consumes its neighbour at render time.
2. **The model never holds a state a terminal cannot display.** A real terminal that
   overwrites half a double-width glyph erases the whole glyph; pyte erases per cell and
   leaves the other half, so the buffer can hold `[2-column glyph][real character]`.
   Rendering that forces a choice between eating the character and shifting the row, and
   a redrawing child alternates between the two. *Symptom: the pane oscillates.* Repair
   it where the write happens, not when reading.
3. **One painted frame shows one pyte generation.** `render_line` is called per row with
   the lock released between rows, while the reader installs whole child frames
   atomically — so a frame can splice generations. *Symptom: rows out of place, healed by
   the next repaint, with nothing in the byte stream that could have moved them.* Pin the
   grid once at the frame boundary (`render_lines`) and read it lock-free.
4. **Every render input that changes a row dirties that row.** Only pyte's `dirty` rows
   are repainted, and Textual serves the rest from its per-line strip cache, so an input
   pyte does not track leaves a *permanently* stale row. Cursor moves, DECTCEM, focus,
   and an overlay on top of the pane are all such inputs. *Symptom: a stale row or a
   ghost cursor that only clears when something else happens to touch that row.*
5. **The user's viewport intent outlives any rebuild.** `DataTable.clear()` zeroes
   `scroll_y`, so a rebuild must restore it — from the position the user owns, not a
   snapshot taken when the rebuild started, and never from a callback that runs a frame
   later. Guarding this with a timer (`is_scrolling`) only holds while frames are fast.
   *Symptom: the list jumps back a row or two while scrolling, worse under load.*
6. **A suppression that ends on a queued callback must COUNT, not flag.** Anything the
   framework defers (`DataTable`'s cursor watcher queues its scroll, so our re-enable
   has to be queued behind it) makes the suppression window outlive the code that
   opened it. Two rebuilds then overlap, and a boolean lets the FIRST window's end
   re-arm the SECOND window's pending work. *Symptom: one frame at the wrong position
   under load, correct again on the next sample — and it does not reproduce on an idle
   machine.* Count the windows and clamp the end at zero.

Diagnostics for all six, and how to attribute a defect to saikai or to the child, are
in [DEBUGGING.md](DEBUGGING.md). For a defect that only shows under load, record every
call that could have caused it WITH its caller and run until it happens: the sixth
invariant was found that way after reasoning about the queue order twice and getting it
wrong both times.

## UI contracts

- Session and pane actions use function keys. Ordinary bare `Ctrl+letter`
  editing keys belong to readline and the hosted agent. The configurable
  pane-release key and app-level quit handling are deliberate exceptions.
- `Select.BLANK` is `False` in supported Textual versions. Omit `value=` when a
  Select must start without a selection.
- Title color groups context; ASCII markers report state. Help and Settings use
  the same `_color_legend` source of truth.
- Live busy/waiting/idle state exists only for saikai-hosted panes. Sessions
  running elsewhere use file registry and transcript heuristics.
- Exactly one component owns the real outer caret, decided by `_native_caret()`:
  saikai drives `?25h`/`?25l` plus DECSCUSR and `render_line` draws nothing, or
  Textual keeps the caret hidden and `render_line` draws the software caret in
  the child's DECSCUSR shape. Both call sites read that one predicate, so "two
  carets" and "no caret" cannot happen. Ownership is Windows, or a POSIX host
  proved to be WSL under Windows Terminal (`WT_SESSION` **and** a WSL proof,
  deferring to `tmux`/`screen`); `SAIKAI_NATIVE_CARET` forces it either way.

### Windows key-boundary limits (not saikai bugs)

Textual's Win32 driver reads the console with `ENABLE_VIRTUAL_TERMINAL_INPUT`
and keeps only `uChar.UnicodeChar`, so some keystrokes are already merged before
saikai sees a key name. Measured on Windows Terminal + ConPTY:

| Pressed | Reaches saikai as | Effect |
| --- | --- | --- |
| `Shift+Enter`, `Ctrl+Enter` | `enter` / `ctrl+j` | submits like Enter |
| `Ctrl+Tab` | `tab` | plain Tab |
| `Alt+1`..`Alt+9`, `Alt+-`, `Alt+=` | `¡ ™ £ ¢ ∞ § ¶ • ª º – ≠` | that character is typed |
| `Alt+b` / `Alt+f` | `ctrl+left` / `ctrl+right` | cursor word-move, not readline |

`encode_key` already emits the correct bytes for the real names
(`shift+enter` → `\x1b[13;2u`, `alt+full_stop` → `\x1b.`), so the fix belongs in
the host, not in saikai: bind the CSI-u form in Windows Terminal's
`settings.json` (`shift+enter` → `sendInput` `[13;2u`, `alt+1` →
`[49;3u`, and so on). Turning `ENABLE_VIRTUAL_TERMINAL_INPUT` off would
recover the records but silently kills mouse reporting and destroys bracketed
paste framing before the application can read it, so it is not an option.

## Verification

Run the full suite with project dependencies before release:

```bash
python -m compileall -q saikai.py saikai_terminal.py saikai_provider.py scripts
uv run python tests/test_config.py
uv run python tests/test_demo_audit.py
uv run python tests/test_demo_fixture.py
uv run python tests/test_keyboard_leader.py
uv run python tests/test_providers.py
uv run python tests/test_pty_backend.py
uv run python tests/test_resource_bounds.py
uv run python tests/test_sort_recency.py
uv run python tests/test_split_divider.py
uv run python tests/test_terminal_protocol.py
uv run python tests/test_terminal_concurrency.py
uv run python tests/test_terminal_watchdog.py
```

After terminal, threading, lock, async, or teardown changes, at minimum run
`tests/test_terminal_protocol.py`, `tests/test_terminal_concurrency.py`,
`tests/test_resource_bounds.py`, `tests/test_terminal_watchdog.py`, and the
real-backend `tests/test_pty_backend.py`. Use Textual `App.run_test()` and
`Pilot` for focus, key, and layout behavior.

## Web mirror (opt-in; read-only by default, opt-in interactive control)

`saikai_mirror.py` (app layer) can mirror the running UI to a browser. It is
OFF unless `SAIKAI_MIRROR` is truthy, binds `127.0.0.1` unless
`SAIKAI_MIRROR_HOST` is set, and authenticates with a per-run token.

Contract:

- A `MirrorDriver` subclass of the platform console driver copies each
  composited frame to the hub (non-blocking, drop-oldest) then calls
  `super().write()`. The local console is byte-identical and untouched.
- `broadcast()` runs on the UI thread and only enqueues — it never takes the
  mirror lock, never marshals, never touches `self._lock` or the PTY. pyte
  feeding and fan-out happen on a separate drain thread.
- A server-side pyte mirror lets a late-joining browser receive a full styled
  snapshot before the live diff stream; client registration and snapshot are
  atomic with respect to the drain feed.
- It is ephemeral: a daemon HTTP thread that dies with the App. No daemon
  outlives the process, no database, no transcript writes.
- It does NOT cover the post-resume foreground Claude: full-takeover resume
  (`action_resume_detached`, or Enter when split-live is disabled) exits the App
  and `subprocess.run(claude_argv)` — Textual/driver/pyte are gone, so the
  mirror goes dark until the App returns. Work in split-live panes to stay
  mirrored.
- Interactive control (default OFF, opt-in): a local `Shift+F12` toggle arms
  browser control; only then do `POST /input` (typed text), `/mouse` (tap/scroll
  → synthesized Textual mouse events), and `/key` (on-screen key bar →
  `events.Key`) inject. Every route is gated identically — Host allow-list +
  per-run write-key header (delivered only over the authenticated SSE, never in a
  URL/QR/log) + Origin fail-closed + control-on — and re-checks the App's
  authoritative `_control_enabled` on the UI thread. The browser cannot enable
  its own control; LAN input additionally requires `SAIKAI_MIRROR_ALLOW_LAN_INPUT`.
  Typed text writes to a focused live pane's PTY, or is replayed as `events.Key`
  for the focused widget (e.g. the search box) when no pane is focused. Control
  idle-auto-disables.
- Geometry is fixed at mount: the mirror's pyte screen is sized once from the
  host terminal. Resizing the real terminal mid-session is NOT yet propagated,
  so late-joiner snapshots and connected browsers keep the mount-time size
  (stale margins) until restart. Resize propagation is a Phase B item.
- Over LAN (`SAIKAI_MIRROR_HOST`), the per-run token travels in the URL query
  (`EventSource` cannot set headers), so it can surface in proxy logs and
  browser history. Acceptable for a trusted home/tethering network only.
