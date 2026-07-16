# Synchronized Output Atomicity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Textual rendering and the Windows IME anchor from observing Claude Code's cursor-hidden/Home intermediate state inside DEC 2026 synchronized-output frames.

**Architecture:** Add a provider-neutral, reader-owned stager before pyte. It releases ordinary output immediately and releases `?2026h ... ?2026l` only as a complete ordered unit; terminal side channels remain live, and a stateful cursor query performs a logged fail-open. The existing coalesced UI repaint stays unchanged after the presentation boundary, while scheduling-only `_sync_deferring` is removed.

**Tech Stack:** Python 3.11+, `re`, `time.monotonic`, pyte, Textual, pywinpty/ConPTY, executable assertion-based test files under `tests/test_*.py`.

## Global Constraints

- Keep provider-specific behavior in `saikai_provider.py`; this change remains entirely provider-neutral in `saikai_terminal.py`.
- Never call `_marshal`, `call_from_thread`, `app.cursor_position`, or a driver write while holding `self._lock`.
- Only the PTY reader thread may mutate synchronized-output staging state or feed pyte.
- Do not change POSIX `ptyprocess` close/join behavior or process-tree reaping.
- Retained synchronized output is limited to 4 MiB of decoded text and 200 ms between received chunks before fail-open.
- Static terminal queries and tracked modes must make progress before `?2026l`; cursor-position DSR may fail-open the retained block.
- Run every `tests/test_*.py` file before completion; terminal changes require `test_terminal_concurrency.py`, `test_resource_bounds.py`, and `test_pty_backend.py`.
- Do not push without an explicit user request.

## File Structure

- Modify `saikai_terminal.py`: synchronized-output parser/stager, raw side-channel split, atomic pyte feed, reader scheduling, EOF flush, bounded diagnostics.
- Modify `tests/test_terminal_concurrency.py`: pure stager tests, queued-repaint race, query progress, EOF/fail-open, mirror ordering, and replacement of the obsolete defer-only test.
- No new runtime module: the helper is private to the provider-neutral terminal layer and is only used there.

---

### Task 1: Pure synchronized-output stager

**Files:**
- Modify: `saikai_terminal.py:793-800`
- Test: `tests/test_terminal_concurrency.py:1189-1210`

**Interfaces:**
- Produces: `_SynchronizedOutputStager(max_chars: int = 4 * 1024 * 1024, max_age: float = 0.2)`
- Produces: `push(chunk: str, now: float | None = None) -> list[tuple[str, str | None]]`
- Produces: `flush(reason: str) -> list[tuple[str, str | None]]`
- Produces: `active: bool`
- Feed-unit tuple meaning: `(text, fail_open_reason)`; normal/plain/closed units use `None`, exceptional releases use `"timeout"`, `"overflow"`, `"cursor-query"`, or `"eof"`.

- [ ] **Step 1: Replace the defer-only test with failing pure stager tests**

Add the following tests and add their calls/`PASS` lines to the module's `if __name__ == "__main__"` runner:

```python
def test_sync_output_stager_holds_split_frame_until_close():
    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=0.2)
    assert s.push("plain", now=1.0) == [("plain", None)]
    assert s.push("\x1b[?2026h\x1b[?25l\x1b[Hhalf", now=1.1) == []
    assert s.active is True
    assert s.push("done\x1b[?25h\x1b[?2026l", now=1.15) == [
        ("\x1b[?2026h\x1b[?25l\x1b[Hhalfdone\x1b[?25h\x1b[?2026l", None)
    ]
    assert s.active is False


def test_sync_output_stager_orders_back_to_back_and_combined_markers():
    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=0.2)
    units = s.push(
        "A\x1b[?25;2026hF1\x1b[?2026lB"
        "\x1b[?2026hF2\x1b[?25;2026lC",
        now=2.0,
    )
    assert units == [
        ("A", None),
        ("\x1b[?25;2026hF1\x1b[?2026l", None),
        ("B", None),
        ("\x1b[?2026hF2\x1b[?25;2026l", None),
        ("C", None),
    ]


def test_sync_output_stager_bounds_and_flushes_once():
    s = rt._SynchronizedOutputStager(max_chars=12, max_age=0.2)
    assert s.push("\x1b[?2026hab", now=3.0) == []
    timeout = s.push("c", now=3.3)
    assert timeout == [("\x1b[?2026hab", "timeout"), ("c", None)]
    assert s.flush("eof") == []

    s = rt._SynchronizedOutputStager(max_chars=12, max_age=1.0)
    overflow = s.push("\x1b[?2026habcdef", now=4.0)
    assert overflow == [("\x1b[?2026habcdef", "overflow")]
    assert s.flush("eof") == []

    s = rt._SynchronizedOutputStager(max_chars=1024, max_age=1.0)
    assert s.push("\x1b[?2026hlast", now=5.0) == []
    assert s.flush("eof") == [("\x1b[?2026hlast", "eof")]
    assert s.flush("eof") == []
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
uv run python -c "import runpy; n=runpy.run_path('tests/test_terminal_concurrency.py'); n['test_sync_output_stager_holds_split_frame_until_close']()"
```

Expected: failure with `AttributeError: module 'saikai_terminal' has no attribute '_SynchronizedOutputStager'`.

- [ ] **Step 3: Implement the minimal sequential stager**

Replace `_SYNC_RE`'s scheduling-only comment and add constants plus a private class. The implementation must use a three-state machine (`outside`, `staging`, `bypass`), scan every DEC private-mode sequence in order, recognize parameter `2026` in combined lists, store retained pieces as `list[str]`, and preserve every byte:

```python
_SYNC_BUFFER_MAX_CHARS = 4 * 1024 * 1024
_SYNC_BUFFER_MAX_AGE = 0.2


class _SynchronizedOutputStager:
    def __init__(self, max_chars=_SYNC_BUFFER_MAX_CHARS,
                 max_age=_SYNC_BUFFER_MAX_AGE):
        self.max_chars = int(max_chars)
        self.max_age = float(max_age)
        self._state = "outside"
        self._parts = []
        self._chars = 0
        self._opened_at = 0.0

    @property
    def active(self):
        return self._state == "staging"

    @staticmethod
    def _is_sync(match):
        return "2026" in match.group(1).split(";")

    def _start(self, marker, now):
        self._state = "staging"
        self._parts = [marker]
        self._chars = len(marker)
        self._opened_at = now

    def _append(self, text):
        if text:
            self._parts.append(text)
            self._chars += len(text)

    def _release(self, reason=None, bypass=False):
        text = "".join(self._parts)
        self._parts = []
        self._chars = 0
        self._opened_at = 0.0
        self._state = "bypass" if bypass else "outside"
        return (text, reason) if text else None

    def flush(self, reason):
        if not self.active:
            return []
        unit = self._release(reason, bypass=True)
        return [unit] if unit else []

    def push(self, chunk, now=None):
        now = time.monotonic() if now is None else float(now)
        out = []
        if self.active and now - self._opened_at > self.max_age:
            out.extend(self.flush("timeout"))

        pos = 0
        plain = []

        def emit_plain():
            if plain:
                out.append(("".join(plain), None))
                plain.clear()

        # Reuse the module's existing DEC private-mode parser so combined
        # parameter lists are interpreted consistently everywhere.
        for match in _DEC_PRIVATE_RE.finditer(chunk):
            if not self._is_sync(match):
                continue
            before = chunk[pos:match.start()]
            marker = match.group(0)
            mode = match.group(2)
            if self._state == "staging":
                self._append(before + marker)
                if mode == "l":
                    unit = self._release()
                    if unit:
                        out.append(unit)
            else:
                plain.append(before)
                plain.append(marker if self._state == "bypass" or mode == "l" else "")
                if self._state == "bypass":
                    if mode == "l":
                        self._state = "outside"
                elif mode == "h":
                    plain.pop()
                    emit_plain()
                    self._start(marker, now)
            pos = match.end()

        tail = chunk[pos:]
        if self._state == "staging":
            self._append(tail)
            if self._chars > self.max_chars:
                out.extend(self.flush("overflow"))
        else:
            plain.append(tail)
        emit_plain()
        return out
```

During GREEN, correct only defects exposed by the stated tests; do not integrate the class yet.

- [ ] **Step 4: Run all three focused tests and verify GREEN**

Run:

```powershell
uv run python -c "import runpy; n=runpy.run_path('tests/test_terminal_concurrency.py'); n['test_sync_output_stager_holds_split_frame_until_close'](); n['test_sync_output_stager_orders_back_to_back_and_combined_markers'](); n['test_sync_output_stager_bounds_and_flushes_once']()"
```

Expected: exit 0 with no output.

- [ ] **Step 5: Run the terminal concurrency file and commit**

Run `uv run python tests/test_terminal_concurrency.py`.

Expected: every printed test reports `PASS`.

Then commit:

```powershell
git add -- saikai_terminal.py tests/test_terminal_concurrency.py
git commit -m "test: define atomic synchronized-output staging"
```

---

### Task 2: Integrate staging before pyte and preserve query progress

**Files:**
- Modify: `saikai_terminal.py:1010-1055`
- Modify: `saikai_terminal.py:1817-1860`
- Modify: `saikai_terminal.py:1968-2190`
- Test: `tests/test_terminal_concurrency.py`

**Interfaces:**
- Consumes: `_SynchronizedOutputStager.push`, `.flush`, and `.active` from Task 1.
- Produces: `_consume(chunk: str) -> bool`, where `True` means at least one unit reached pyte.
- Produces: `_consume_ready(chunk: str) -> None`, reader-thread-only pyte/mirror feed for one complete unit.
- Produces: `_answer_static_queries(chunk: str) -> None` and `_answer_cursor_queries(chunk: str) -> None`.

- [ ] **Step 1: Write the failing queued-repaint race test**

Add a test using real pyte under `uv run`:

```python
def test_sync_output_next_open_frame_cannot_mutate_queued_complete_frame():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(30, 6, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: None

    frame_a = "\x1b[?2026h\x1b[5;10HREADY\x1b[?25h\x1b[?2026l"
    assert t._consume(frame_a) is True
    with t._lock:
        stable = (t._screen.cursor.x, t._screen.cursor.y,
                  bool(t._screen.cursor.hidden))

    frame_b_open = "\x1b[?2026h\x1b[?25l\x1b[Hpartial"
    assert t._consume(frame_b_open) is False
    with t._lock:
        observed = (t._screen.cursor.x, t._screen.cursor.y,
                    bool(t._screen.cursor.hidden))

    assert observed == stable
    assert observed[2] is False
    assert observed[:2] != (0, 0)
```

- [ ] **Step 2: Write failing query-progress tests**

```python
def test_static_query_answers_before_sync_block_closes():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    sent = []
    t._send_to_child = lambda data: sent.append(data)
    t._marshal = lambda fn: fn()

    assert t._consume("\x1b[?2026h\x1b[c") is False
    assert sent == ["\x1b[?6c"]


def test_cursor_query_fail_opens_sync_block_then_reports_new_cursor():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    sent = []
    t._send_to_child = lambda data: sent.append(data)
    t._marshal = lambda fn: fn()

    assert t._consume("\x1b[?2026h\x1b[3;7H\x1b[6n") is True
    assert sent == ["\x1b[3;7R"]
    assert t._sync_output.active is False
```

- [ ] **Step 3: Run the three tests and verify RED**

Run them via `runpy.run_path` as in Task 1.

Expected failures on current integration:

- `_consume(frame_a) is True` fails because `_consume` returns `None`;
- frame B mutates pyte to hidden/Home;
- a static query inside a held block is not answered by the not-yet-written split;
- cursor DSR cannot perform the controlled fail-open.

- [ ] **Step 4: Split query handling without changing response bytes**

Refactor `_answer_queries` into static and cursor-dependent halves, preserving the existing reply formats:

```python
def _answer_static_queries(self, chunk: str) -> None:
    out = []
    if _DA_RE.search(chunk):
        out.append("\x1b[?6c")
    for _priv, _kind in _DSR_RE.findall(chunk):
        if _kind == "5":
            out.append("\x1b[0n")
    for _mode in _DECRQM_RE.findall(chunk):
        out.append(f"\x1b[?{_mode};{'2' if _mode == '2026' else '0'}$y")
    if _XTVERSION_RE.search(chunk):
        out.append("\x1bP>|saikai\x1b\\")
    for _code in _OSC_COLOR_Q_RE.findall(chunk):
        rgb = "1e1e/1e1e/1e1e" if _code == "11" else "c0c0/c0c0/c0c0"
        out.append(f"\x1b]{_code};rgb:{rgb}\x07")
    if out:
        response = "".join(out)
        self._marshal(lambda r=response: self._send_to_child(r))


def _answer_cursor_queries(self, chunk: str) -> None:
    out = []
    for private, kind in _DSR_RE.findall(chunk):
        if kind == "6":
            row, col = self._cursor_rowcol()
            out.append(f"\x1b[{private}{row};{col}R")
    if out:
        response = "".join(out)
        self._marshal(lambda r=response: self._send_to_child(r))


def _answer_queries(self, chunk: str) -> None:
    self._answer_static_queries(chunk)
    self._answer_cursor_queries(chunk)
```

Keep `_answer_queries` as the compatibility wrapper used by the existing direct unit test.

- [ ] **Step 5: Integrate the stager while preserving current raw side channels**

Initialize `self._sync_output = _SynchronizedOutputStager()` next to `_esc_carry`.

In `_consume`:

1. keep PTY capture, `_esc_carry`, Windows sentinel scrub, private-SGR scrub, and Kitty-keyboard scrub first;
2. keep bracketed-paste, tracked DEC modes, OSC52, and notifications on the raw scrubbed chunk;
3. call `_answer_static_queries(chunk)` before staging;
4. call `units = self._sync_output.push(chunk)`;
5. if `_DSR_RE` contains kind `6` and the stager is still active, append `flush("cursor-query")`;
6. feed every returned unit in order via `_consume_ready`, logging non-`None` reasons;
7. call `_answer_cursor_queries(chunk)` only after ready units have reached pyte;
8. return whether any unit was fed.

Move the existing pyte lock/feed, alt-screen handling, mirror tee, status classification,
query-independent bell handling, and `_scr_ver` invalidation into `_consume_ready` without
changing their relative lock/marshal ordering. Do not call a marshal under `_lock`.

- [ ] **Step 6: Remove the scheduling-only race**

Delete `_sync_deferring`. In `_read_loop`, use the boolean result:

```python
changed = self._consume(chunk)
if changed and self._scroll == 0 and not self._frozen:
    self._schedule_pane_refresh()
```

An opening block returns `False`; a closed frame returns `True`. A queued repaint can run
after the next block opens because that open block no longer mutates pyte.

- [ ] **Step 7: Run focused tests and verify GREEN**

Run the three new Task 2 tests plus `test_answer_queries_responds_to_terminal_probes`.

Expected: all exit 0; response bytes remain identical to the old test.

- [ ] **Step 8: Run mandatory focused suites and commit**

Run:

```powershell
uv run python tests/test_terminal_concurrency.py
uv run python tests/test_resource_bounds.py
```

Expected: every test prints `PASS`, with no traceback or hang.

Then commit:

```powershell
git add -- saikai_terminal.py tests/test_terminal_concurrency.py
git commit -m "fix: present synchronized PTY frames atomically"
```

---

### Task 3: EOF, fail-open, mirror ordering, and diagnostics

**Files:**
- Modify: `saikai_terminal.py:1817-1870`
- Modify: `saikai_terminal.py:2000-2190`
- Test: `tests/test_terminal_concurrency.py`

**Interfaces:**
- Consumes: Task 2 `_consume_ready` and `_SynchronizedOutputStager.flush`.
- Produces: `_flush_sync_output(reason: str) -> bool`, reader-thread-only.
- Preserves: mirror seed-before-stream ordering inside `self._lock`.

- [ ] **Step 1: Add failing EOF and mirror-order tests**

```python
def test_sync_output_eof_flushes_retained_frame_once():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: None

    assert t._consume("\x1b[?2026hEOF-TEXT") is False
    assert t._flush_sync_output("eof") is True
    assert "EOF-TEXT" in "\n".join(rt._pyte_grid_lines(t._screen))
    assert t._flush_sync_output("eof") is False


def test_sync_output_mirror_gets_closed_block_once_in_order():
    import pyte

    t = rt.AgentTerminal(["agent"], status_classifier=lambda _txt, _title: "idle")
    t._screen = rt._HistoryScreenBase(20, 5, history=20)
    t._stream = pyte.Stream(t._screen)
    t._sync_output = rt._SynchronizedOutputStager()
    t._marshal = lambda fn: None
    mirrored = []
    t._mirror_tee = lambda chunk: mirrored.append(chunk)

    assert t._consume("pre\x1b[?2026hA") is True
    assert mirrored == ["pre"]
    assert t._consume("B\x1b[?2026lpost") is True
    assert mirrored == ["pre", "\x1b[?2026hAB\x1b[?2026l", "post"]
```

- [ ] **Step 2: Run both tests and verify RED**

Expected: `_flush_sync_output` is missing and/or retained EOF text is absent.

- [ ] **Step 3: Implement reader-only flush and bounded logging**

```python
def _flush_sync_output(self, reason: str) -> bool:
    changed = False
    for text, fail_reason in self._sync_output.flush(reason):
        if fail_reason:
            _log(f"sync-output fail-open: reason={fail_reason} chars={len(text)}")
        self._consume_ready(text)
        changed = True
    return changed
```

Use the same log format for timeout/overflow/cursor-query units returned by `push`.
Do not include PTY text in the normal bounded log.

In `_read_loop`'s reader-thread `finally`, call `_flush_sync_output("eof")` before
`_finalize()`. `_finalize()` already marshals the final refresh, so do not add a second
uncoalesced UI call.

- [ ] **Step 4: Update comments and remove obsolete state completely**

Remove `_in_sync_update` and `_sync_started` initialization/use. Update `_SYNC_RE`/
synchronized-output comments, `_read_loop` comments, and the replaced test name so none
claim that saikai feeds pyte continuously or only defers repaint scheduling.

Keep the `busy` IME freeze and settle transition unchanged for non-2026 children.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run Task 1-3 tests, then:

```powershell
uv run python tests/test_terminal_concurrency.py
uv run python tests/test_resource_bounds.py
uv run python tests/test_pty_backend.py
```

Expected: all tests print `PASS`; the real PTY backend spawns, resizes, reads, and reaches EOF.

- [ ] **Step 6: Compile and commit**

Run:

```powershell
uv run python -m py_compile saikai.py saikai_terminal.py saikai_provider.py saikai_mirror.py
```

Expected: exit 0 with no output.

Then commit:

```powershell
git add -- saikai_terminal.py tests/test_terminal_concurrency.py
git commit -m "test: cover synchronized-output progress and teardown"
```

---

### Task 4: Full verification and Windows IME acceptance

**Files:**
- Modify only if evidence exposes a specific remaining defect; begin a new RED/GREEN cycle before any such edit.
- Verify: every `tests/test_*.py`
- Verify: `%TEMP%\saikai_ime_debug_*.txt` and `%TEMP%\saikai_pty_capture_*.txt`

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: test output and before/after real-device counts; no push.

- [ ] **Step 1: Run exactly the complete CI test glob**

PowerShell equivalent of the repository's shell loop:

```powershell
$failed = $false
Get-ChildItem tests\test_*.py | Sort-Object Name | ForEach-Object {
    Write-Host "== $($_.FullName) =="
    uv run python $_.FullName
    if ($LASTEXITCODE -ne 0) { $failed = $true; break }
}
if ($failed) { exit 1 }
```

Expected: every file completes successfully; do not summarize a failing file as green.

- [ ] **Step 2: Start a fresh instrumented Windows Terminal run**

Launch directly (not through `Start-Process -ArgumentList`) with unique explicit paths:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$env:SAIKAI_IME_ANCHOR = '1'
$env:SAIKAI_IME_DEBUG = "$env:TEMP\saikai_ime_debug_atomic_$stamp.txt"
$env:SAIKAI_PTY_CAPTURE = "$env:TEMP\saikai_pty_capture_atomic_$stamp.txt"
wt.exe -w new -d 'C:\Users\masay\CLI\saikai' pwsh.exe -NoExit -Command 'uv run saikai.py'
```

Verify the `pwsh -> uv -> python -> saikai.py` process chain before asking for input.

- [ ] **Step 3: Reproduce the same user flow**

In a focused Claude split-live pane, with the search box closed:

1. type and convert Japanese at the idle prompt;
2. observe cursor tracking and candidate placement;
3. allow at least one full-screen Claude redraw;
4. repeat with the search Input focused to confirm its caret remains authoritative;
5. exit saikai so EOF/finalize paths run.

- [ ] **Step 4: Quantify before/after evidence**

Parse the new logs and report:

- raw `?2026h/?2026l` and `?25h/?25l` counts;
- IME `HIDE`, anchor, and HIDE↔anchor transitions;
- anchors at the content-region origin;
- normal prompt anchor positions while Japanese text advances;
- any `sync-output fail-open` log entries and their reasons.

Acceptance:

- no visible half-frame/layout tear;
- no origin anchor caused by a partial 2026 frame;
- no native-cursor thrashing caused by partial frame presentation;
- prompt anchor advances with Japanese input after completed frames;
- search Input focus still owns its cursor.

- [ ] **Step 5: Verify final status and stop**

Run:

```powershell
git status --short --branch
git log -5 --oneline
```

Expected: only intentional commits, clean worktree, `master` ahead of `origin/master`, and no push performed.

If real-device evidence fails, do not stack a blind patch. Record the exact failing trace condition, return to systematic-debugging Phase 1, write one new failing test, and make one minimal change.
