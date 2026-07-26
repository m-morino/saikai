# Terminal correctness hardening implementation plan

> **Required execution workflow:** use `superpowers:subagent-driven-development`.
> Each task is test-first, committed separately, and receives a task-scoped
> specification/quality review before the next task.

**Goal:** Correct every confirmed Windows Terminal/ConPTY, WezTerm, and POSIX
terminal audit finding without violating split-live concurrency invariants.

**Design:** `docs/superpowers/specs/2026-07-26-terminal-correctness-design.md`

**Tech stack:** Python 3.11+, Textual 8.x, pyte 0.8.x, Rich 15.x, `regex`,
pywinpty on Windows, ptyprocess on POSIX, plain direct-run test scripts.

## Global constraints

- Work only in the `codex/fix-terminal-audit` worktree.
- Read `CONTRIBUTING.md` and `docs/ARCHITECTURE.md` before editing.
- Use `apply_patch` for source/test/document edits.
- Preserve unrelated user changes.
- Never marshal or call `call_from_thread` while `AgentTerminal._lock` is held.
- Never close/join a POSIX PTY or call `pywinpty.close(force=True)` on the UI
  thread.
- Track and bounded-join every process reap/escalation helper.
- Keep PTY-triggered UI work coalesced.
- Keep provider-specific behavior out of `saikai_terminal.py`.
- Every regression starts RED: add the test, run it, and record the expected
  failure before production code. Then run the focused test GREEN.
- Each direct-run test file must retain an `if __name__ == "__main__"` runner.
- Do not weaken an assertion or delete coverage to make a test pass.

## Common verification commands

Run commands from the worktree root with:

```powershell
.\.venv\Scripts\python.exe -m py_compile saikai.py saikai_terminal.py saikai_provider.py
.\.venv\Scripts\python.exe tests\test_terminal_concurrency.py
.\.venv\Scripts\python.exe tests\test_resource_bounds.py
.\.venv\Scripts\python.exe tests\test_terminal_watchdog.py
.\.venv\Scripts\python.exe tests\test_pty_backend.py
```

---

### Task 1: Dependency parity and incremental VT tokenizer

**Files:**

- Modify: `pyproject.toml`
- Modify: `saikai.py` (PEP 723 dependency block only)
- Modify: `saikai_terminal.py` (token types and incremental tokenizer)
- Create: `tests/test_terminal_protocol.py`
- Modify: `tests/test_resource_bounds.py`

**Requirements:**

- Add `regex` to installed dependencies.
- Make the PEP 723 block include `regex`, `segno`, and `cryptography`, matching
  runtime package dependencies.
- Introduce one provider-neutral incremental tokenizer for decoded text.
- Recognize ordinary text/control data, simple ESC, CSI parameter/intermediate/
  final grammar, OSC BEL/ST, DCS ST plus defensive BEL, and C1 equivalents.
- Preserve exact raw text on emitted tokens.
- Carry incomplete tokens across calls, including `ESC[?2026$` followed by `p`.
- Bound every carry. Oversize/malformed tokens fail open rather than retain
  indefinitely.
- Do not yet replace `AgentTerminal._consume`; this task proves the parser in
  isolation.

**RED tests:**

- Split every byte boundary of representative CSI, DECRQM, DECSCUSR, OSC, DCS,
  simple ESC, and C1 sequences and compare with one-shot tokenization.
- Assert CSI `$` is an intermediate and `p` the final byte.
- Assert OSC 9/52/777/99 accept BEL and ST.
- Assert DCS payload content never becomes an OSC/CSI token.
- Assert carry and dropped-string accounting stay under configured caps.
- Assert dependency lists contain the required parity entries.

**GREEN verification:**

```powershell
.\.venv\Scripts\python.exe tests\test_terminal_protocol.py
.\.venv\Scripts\python.exe tests\test_resource_bounds.py
```

**Commit:** `refactor: add bounded incremental VT tokenizer`

---

### Task 2: Ordered VT dispatch, modes, queries, sync timeout, and input protocol

**Files:**

- Modify: `saikai_terminal.py`
- Modify: `tests/test_terminal_protocol.py`
- Modify: `tests/test_terminal_concurrency.py`

**Requirements:**

- Route `_consume` through Task 1's token stream and remove `_esc_carry`,
  independent OSC52 carry, stateless notification scans, static query pre-scan,
  and chunk-final private-mode pre-scan.
- Dispatch every side effect at its stream position. Multiple queries in one
  token batch must each receive one reply in request order.
- DSR 6 must observe preceding presentation but not trailing presentation.
  DECRQM must observe mode state exactly at the query.
- Parse combined DECSET/DECRST lists per parameter. Track/report modes 1, 25,
  47/1047/1049, 1000/1002/1003, 1004, 1006, 2004, and 2026.
- Keep mouse tracking as one exclusive protocol slot.
- Answer DECRQM inside synchronized output without deadlock. Flush retained
  presentation before a cursor-position query.
- Arm one generation-checked timeout when synchronized output opens. A quiet
  child must fail open once after `_SYNC_BUFFER_MAX_AGE`; a clean close, EOF, or
  teardown must cancel/retire the deadline. Timeout-fed output uses existing
  coalesced UI scheduling outside `_lock`.
- Return a conservative truthful Primary DA with no sixel/DRCS/macro/rectangular
  editing claims. XTVERSION must identify saikai. Repeated DA/DA2/DSR5/color
  queries are not collapsed.
- Parse Kitty keyboard query/set/push/pop into a bounded state stack and return
  the current flags on query. Strip these tokens from presentation only after
  their protocol effect is applied.
- Make `encode_key` accept DECCKM/Kitty state while preserving backwards-
  compatible defaults. Application cursor/Home/End use SS3; negotiated Kitty
  represents modified Enter/control/printable keys; legacy Alt uses `character`
  to preserve punctuation and shifted symbols.
- Parse OSC 9/777/99 after complete OSC assembly with BEL/ST. Gate OSC 52 through
  a helper that requires a live, visible, attached, focused pane on the active
  app screen.

**RED tests:**

- `DSR6 -> DA -> DSR5 -> DA` returns CPR, DA, OK, DA in that order.
- `set -> DECRQM -> reset -> DECRQM` in one `_consume` reports set then reset for
  every tracked mode, including 1004/2004/2026.
- `?2004;1004h` and `?1049;25h` update both parameters.
- Split `CSI ? 2026 $ p` replies and never leaves the sync stager active.
- Quiet sync frame expires with no later `push`, repaints once, and close/timeout
  races do not double-feed.
- DA omits unsupported capability codes; repeated queries all reply.
- Kitty query/set/push/pop stack semantics, modified Enter/control, DECCKM
  arrows/Home/End, and Alt punctuation.
- OSC notifications work at every split point with BEL/ST.
- Focused OSC52 writes once; background, hidden, detached, dead, and inactive
  panes do not write host or mirror clipboards.

**GREEN verification:**

```powershell
.\.venv\Scripts\python.exe tests\test_terminal_protocol.py
.\.venv\Scripts\python.exe tests\test_terminal_concurrency.py
```

**Commit:** `fix: dispatch terminal protocols in stream order`

---

### Task 3: Grapheme-correct screen, real alternate buffer, and presentation state

**Files:**

- Modify: `saikai_terminal.py`
- Modify: `tests/test_flag_width.py`
- Modify: `tests/test_terminal_concurrency.py`
- Modify: `tests/test_pane_dump.py`
- Modify: `tests/test_resource_bounds.py`

**Requirements:**

- Segment printable presentation with `regex` `\X`.
- Retain only the potentially extensible trailing cluster; commit it at a
  control/query/frame/snapshot/resize/EOF boundary or one short
  generation-checked idle deadline.
- Store one complete grapheme in its leading cell, compute the same cell advance
  as Rich's renderer, and maintain deterministic continuation cells.
- Cover combining marks, VS15/VS16, emoji modifiers, ZWJ families, regional-
  indicator flags, and keycaps, including read splits and the right margin.
- Ensure native Textual cursor position and CPR observe committed graphemes.
- Replace destructive one-buffer alternate reset with separate main/alternate
  screen+stream pairs. Preserve main cursor/history exactly, keep alternate
  history bounded, make toggles idempotent, and resize both buffers.
- Track DECSCUSR and apply supported Textual native cursor shape together with
  screen-space position; restore the default on focus loss/hide/death/unmount.
- Fix scrollback pinning at a full `deque(maxlen=...)` with a stable generation
  or displayed snapshot, so rendered and copied cells remain identical.
- Resize must bump `_scr_ver`, invalidate screen/status caches, clamp scroll and
  CPR row/column, and schedule IME/mirror synchronization outside the lock.
- Replace the global unbounded truecolor cache with a bounded LRU or non-cached
  truecolor path.
- Invalidate/reclassify stable status once `last_input_ts + 4s` expires even with
  no screen changes. All local/mirror paste paths stamp the same input clock.

**RED tests:**

- For `A❤️B`, `A1️⃣B`, `A👨‍👩‍👧‍👦B`, `A🇯🇵B`, skin-tone emoji, and decomposed
  combining text, assert pyte cursor advance equals `rich.cells.cell_len`.
- Repeat each case split at every codepoint/read boundary and at the final column.
- MAIN -> ALT -> MAIN restores MAIN content/history/cursor; combined/repeated
  47/1047/1049 transitions do not erase it.
- DECSCUSR shape follows focused visible cursor and resets on every exit path.
- A full three-line history deque stays visually/copy pinned while new lines
  evict old entries.
- Resize changes screen version/cache identity and clamps CPR to new dimensions.
- Generate more unique truecolors than the cache cap and assert the bound.
- Fake-clock recent-input prompt reclassifies after four seconds without output.
- `on_paste`, `paste_text`, and mirror injection stamp the same status deadline.

**GREEN verification:**

```powershell
.\.venv\Scripts\python.exe tests\test_flag_width.py
.\.venv\Scripts\python.exe tests\test_terminal_concurrency.py
.\.venv\Scripts\python.exe tests\test_pane_dump.py
.\.venv\Scripts\python.exe tests\test_resource_bounds.py
```

**Commit:** `fix: make terminal presentation grapheme correct`

---

### Task 4: Bounded serialized PTY writer and nested-terminal environment

**Files:**

- Modify: `saikai_terminal.py`
- Modify: `saikai.py` only where mirror input bypasses `AgentTerminal` writer
- Modify: `tests/test_terminal_concurrency.py`
- Modify: `tests/test_resource_bounds.py`
- Modify: `tests/test_pty_backend.py`

**Requirements:**

- Send every PTY write through one per-pane FIFO writer worker. No caller,
  including a one-byte key or terminal query reply, may call blocking
  `pty.write()` inline.
- Use a deque/condition and maintained UTF-8 byte count; enqueue and accounting
  are O(1). Apply the cap to encoded bytes, not Python characters.
- Preserve order across key, paste, mouse, mirror input, focus event, resize-
  related query reply, and reader-generated terminal replies.
- Enqueue never blocks the UI/reader thread. A full queue or oversized item is
  rejected/logged without corrupting accounting or reordering accepted writes.
- Stop acceptance and wake/retire the writer on natural EOF and kill. Make its
  shutdown bounded and observable in resource tests.
- Route mirror injection through the public pane write method rather than direct
  `_pty.write`.
- Expand exact/prefix child-environment scrubbing for `WEZTERM_*`, `TMUX`,
  `TMUX_PANE`, `STY`, Kitty IPC/identity variables, Alacritty socket/window
  variables, Konsole/GNOME terminal identifiers, and `TERMINFO*`.
- Add deliberate saikai identity and preserve the existing Windows-only
  `WT_SESSION` compatibility policy. On POSIX do not leak it. Normalize the child
  encoding contract to UTF-8 without changing language preference.

**RED tests:**

- A fake PTY whose `write()` blocks cannot block `on_key`, paste, mouse, or query
  reply for one byte, 4096 characters, or multibyte text.
- Interleaved producers reach the fake PTY in accepted FIFO order.
- `"界" * 2000` is accounted as 6000 UTF-8 bytes.
- Queue saturation/teardown clears accounting and leaves no unbounded writer
  workers.
- Real PTY smoke sends ordered key/paste data and exits cleanly.
- Environment matrix removes all exact/prefix host variables on Windows/POSIX
  while retaining unrelated variables and the intended TERM/UTF-8 contract.

**GREEN verification:**

```powershell
.\.venv\Scripts\python.exe tests\test_terminal_concurrency.py
.\.venv\Scripts\python.exe tests\test_resource_bounds.py
.\.venv\Scripts\python.exe tests\test_pty_backend.py
```

**Commit:** `fix: serialize and bound all PTY writes`

---

### Task 5: Exact EOF ownership and cross-platform process-tree reap

**Files:**

- Modify: `saikai_terminal.py`
- Modify: `saikai.py`
- Modify: `tests/test_terminal_concurrency.py`
- Modify: `tests/test_resource_bounds.py`
- Modify: `tests/test_pty_backend.py`

**Requirements:**

- Guard `(pty, pid, generation)` with a lifecycle lock. Explicit kill and natural
  reader EOF atomically detach only their owned generation.
- On natural EOF, flush parser/sync/grapheme tails, retain the final screen,
  detach PTY/PID before announcing death, then perform close/wait/reap off the UI
  thread. Later `kill()`/unmount must be a no-op for the old PID.
- Windows `pywinpty.close(force=True)`, terminate fallback, and `taskkill /T`
  must all run on a tracked reap thread, never on the UI thread.
- POSIX explicit kill posts only non-blocking group signals on the caller thread.
  The tracked reaper waits the direct child, checks process-group liveness, sends
  SIGKILL when descendants survive even if the direct child is already dead,
  and closes the PTY off-thread.
- Do not spawn an untracked close helper. If a close must be isolated, register
  that helper and include it in bounded joins.
- Register the POSIX `_kill_agent_process` SIGKILL escalation in a saikai-owned
  reap registry and join it on every normal shutdown path.
- Preserve idempotence and the existing PID/process-start identity checks.

**RED tests:**

- Natural EOF immediately sets `_pty is None`/`_pid is None`, keeps the final
  screen, reaps once, and delayed unmount sends no signal/taskkill.
- Race natural EOF against explicit kill repeatedly; exactly one generation owns
  cleanup and no reused PID is touched.
- Fake Windows close records a non-UI thread and one tracked reaper.
- POSIX direct child exit with a surviving slave-holding grandchild triggers
  group escalation and all tracked workers join.
- Agent SIGTERM-ignore escalation is registered, SIGKILLs after grace, and
  bounded shutdown waits for it.
- Repeated create/exit/kill cycles keep thread/fd/handle counts bounded.

**GREEN verification:**

```powershell
.\.venv\Scripts\python.exe tests\test_terminal_concurrency.py
.\.venv\Scripts\python.exe tests\test_resource_bounds.py
.\.venv\Scripts\python.exe tests\test_pty_backend.py
```

**Commit:** `fix: detach and reap PTYs on every exit path`

---

### Task 6: Make the Windows terminal watchdog failure-safe

**Files:**

- Modify: `saikai.py`
- Modify: `tests/test_terminal_watchdog.py`
- Modify: `tests/test_resource_bounds.py`

**Requirements:**

- `_win_pid_index()` returns `None` for snapshot/enumeration failure and a dict
  only for a conclusive successful enumeration.
- Startup does not arm the watchdog from an inconclusive snapshot.
- Polling treats a snapshot missing saikai's own PID as inconclusive.
- A live anchor clears misses; a conclusive missing anchor increments them; an
  inconclusive result clears the streak and never kills.
- Extract/inject the poll decision enough for deterministic tests without
  launching an immortal thread.
- Preserve two consecutive conclusive misses before resetting modes, taskkilling
  only saikai's own tree, and hard-exiting.
- Reset alternate-screen/cursor/mouse/paste modes before the hard exit.
- Update every `_win_pid_index` caller to handle `None` as unknown.

**RED tests:**

- `fail, fail`, `miss, fail, miss`, `miss, fail, miss, miss`, and snapshots
  missing self never kill.
- Two consecutive conclusive self-present/anchor-missing snapshots kill once.
- A live snapshot between misses resets the streak.
- Snapshot create/first/next exceptions all return `None`; successful empty
  enumeration remains `{}`.
- Live-session PID validation does not declare a process gone solely because
  enumeration is inconclusive.

**GREEN verification:**

```powershell
.\.venv\Scripts\python.exe tests\test_terminal_watchdog.py
.\.venv\Scripts\python.exe tests\test_resource_bounds.py
```

**Commit:** `fix: treat watchdog snapshot failures as inconclusive`

---

### Task 7: Whole-branch integration, documentation, and independent review

**Files:**

- Modify: `docs/ARCHITECTURE.md`
- Modify: `CONTRIBUTING.md` only if the development contract changed
- Modify: tests/source only for defects found by integration or review

**Requirements:**

- Document the ordered tokenizer, real buffer pair, all-write worker, ownership
  generation, natural-EOF cleanup, and timeout/cache bounds.
- Verify no provider-specific launch/status logic moved into
  `saikai_terminal.py`.
- Search mechanically for direct `_pty.write`, inline PTY close/terminate,
  untracked reap/escalation thread creation, `_marshal` under `_lock`, obsolete
  `_esc_carry`, and unbounded terminal caches/queues.
- Run compilation and every `tests/test_*.py` script from a clean command
  invocation. Preserve the complete output and exit codes.
- Run an independent whole-branch code review against the design and audit list.
  Fix all load-bearing findings and re-run affected tests, then the full suite.
- Confirm `git diff --check` and a clean worktree.

**Full-suite command:**

```powershell
$ErrorActionPreference = 'Stop'
.\.venv\Scripts\python.exe -m py_compile saikai.py saikai_terminal.py saikai_provider.py saikai_mirror.py
Get-ChildItem tests\test_*.py | Sort-Object Name | ForEach-Object {
    & .\.venv\Scripts\python.exe $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "failed: $($_.Name)" }
}
git diff --check
```

**Commit:** `docs: record terminal correctness invariants`

## Completion criteria

- Every task-scoped review has both specification and quality approval.
- Every confirmed audit finding has a regression test and implementation.
- Mandatory and full suites pass in fresh invocations.
- Final independent review has no open P0/P1/P2 finding.
- Branch contains only intentional commits and the worktree is clean.
