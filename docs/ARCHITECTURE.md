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
2. `AgentTerminal` spawns the PTY and starts its reader.
3. PTY chunks update the screen under `self._lock`.
4. UI refresh/status work is coalesced and marshalled after releasing the lock.
5. Teardown signals the process tree and reaps it off the UI thread.

Windows uses ConPTY through `pywinpty`. POSIX platforms use `ptyprocess`.

## Concurrency invariants

Violating these rules can hard-freeze the UI or orphan agent worker processes:

1. Never call `call_from_thread`, `self._marshal`, or another blocking
   cross-thread operation while holding `self._lock`. Compute under the lock;
   marshal after releasing it.
2. Never join the reader thread from Textual's UI thread.
3. On POSIX, never call `ptyprocess.close()` or `terminate()` on the UI thread.
   A reader can hold `io.BufferedRWPair`'s read lock while blocking in `read1()`;
   closing from the UI thread then waits on the same lock. Signal the child
   first and perform the blocking close on the reap thread.
4. Track every process-tree reap and join outstanding reap threads at process
   exit. Otherwise saikai can exit before descendants are terminated.
5. Coalesce PTY-driven repaint and status work. A streaming agent can emit many
   chunks and status transitions per second.

Do not add locking to fix a cosmetic race unless the lock ordering has been
proved and covered by a regression test.

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
uv run python tests/test_terminal_concurrency.py
uv run python tests/test_terminal_watchdog.py
```

After terminal, threading, lock, async, or teardown changes, at minimum run
`tests/test_terminal_concurrency.py`, `tests/test_resource_bounds.py`, and the
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
