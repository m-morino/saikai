# saikai Context-Lifecycle Assistant — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface each live Claude Code session's *real* context fill across all panes, make "compact / checkpoint-and-refresh" a safe one-key action, and keep a recovery pointer back to a cleared session.

**Architecture:** saikai reads ground-truth token counts from the transcript JSONL (`message.usage`), shows a per-focused-pane gauge in the statusbar, and orchestrates standard commands (`/compact`, `/handoff`, `/clear`) into the live pane via a bracketed-paste helper. Claude Code owns the window; saikai never estimates when real numbers exist, and never clears autonomously.

**Tech Stack:** Python 3.11+, Textual, pyte; no-pytest (`uv run python tests/test_x.py`; RED = AssertionError/traceback, GREEN = the `PASS`/`OK` print). Commit identity m-morino; every commit ends with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer. Push only on explicit request.

---

## Verified codebase facts (ground every step in these; follow the REAL code if it differs)

- **Transcript usage shape (verified on a real transcript):** each assistant record is `{"message": {"model": "...", "usage": {"input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens", ...}}}`. **Live context = `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`** of the LAST record bearing `usage`. Measured live: 719,882 tokens on this repo's session.
- **Window detection correction:** `message.model` is the BASE id (`"claude-opus-4-8"`) — it does NOT carry the `[1m]` suffix, so you cannot detect a 1M window from the model string. **Infer the window from the observed token count** (a 720K reading cannot be a 200K window). Tiers: 200_000, then 1_000_000.
- **saikai.py anchors:** `_read_json`/`_write_json` (~525), `_load_custom_titles`/`_set_custom_title` + module cache globals + `CUSTOM_TITLES_FILE = CACHE_DIR / ...` (~547/627/644) — the sidecar pattern to clone; `_load_severity`/`_LOAD_COL`/`_live_ram_segment` (~2924-2954) — severity colours + the live statusbar segment; the statusbar builder assembles `text = (...{live_str}...{_kb})` then `self.query_one("#statusbar", Static).update(text)` (the live segment is built under `if self._live is not None:`); `_focused_terminal()` (~5129) returns the focused live `AgentTerminal` (has `.sid`); `_restat_live` / `_poll_live_status` (~6013-6055, UI thread, ~1.5s, stats each live jsonl); `_MODAL_BLOCKED_ACTIONS` frozenset + the `BINDINGS` list (~3635-3756); `_sid_index[sid]` dicts carry `"jsonl_path"`.
- **saikai_terminal.py anchors:** `AgentTerminal.on_paste` (~893-905) writes `"\x1b[200~"+text+"\x1b[201~"` to `self._pty` when `self._bracketed_paste` (set in `_consume` on `?2004h`); `encode_key`/`on_key` (~378-383, 842-852) is the per-key path (`enter` -> `\r`); the pane carries a live status (`self._live.statuses()` -> values incl. `"waiting"`/`"busy"`/`"idle"`/`"done"`/`"dead"`; verify the exact "busy/streaming" value when you build the idle gate).
- **Concurrency invariants (CLAUDE.md / docs/ARCHITECTURE.md):** never marshal while holding `self._lock`; never close a POSIX `ptyprocess` on the UI thread; PTY writes happen on the UI thread; a blocking wait on the UI thread freezes every pane (reader threads block on `call_from_thread`). Run `tests/test_terminal_concurrency.py` + `tests/test_pty_backend.py` after any live-pane change.
- **Test fixture pattern:** `tests/test_keyboard_leader.py` points env (`USERPROFILE`/`HOME`/`APPDATA`/`LOCALAPPDATA`) at a throwaway dir BEFORE importing saikai, and drives the real app by monkeypatching `App.run` with a `run_test()` Pilot closure. `tests/test_resource_bounds.py` is the home for pure resource-math unit tests.

---

## Files

- **Modify `saikai.py`:** new pure fns `_ctx_tokens_from_jsonl`, `_ctx_window_for`, `_ctx_gauge_segment`; statusbar wiring; lineage sidecar `_load_lineage`/`_set_lineage` + `LINEAGE_FILE`; `action_open_parent`; `action_context_refresh` + a Shift+F11 binding + `_MODAL_BLOCKED_ACTIONS` entry; the b2 tick state machine.
- **Modify `saikai_terminal.py`:** `AgentTerminal.paste_text(text)` + `submit()` helpers (reuse the `on_paste` bracketed-paste logic).
- **Modify `saikai_mirror.py`:** add a `data-k="shift+f11"` key-bar button.
- **Tests:** add to `tests/test_resource_bounds.py` (pure ctx-math + lineage), `tests/test_keyboard_leader.py` (Pilot: gauge in statusbar, refresh idle-gate, open-parent), `tests/test_mirror_input.py` (key-bar button).

---

## Task 1 (a): ground-truth context-token read

**Files:** Modify `saikai.py` (new pure fn near `_ram_per_pane_mb`, ~2917). Test: `tests/test_resource_bounds.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_resource_bounds.py`, and register in `__main__`)

```python
def test_ctx_tokens_reads_last_usage_block(tmp_path=None):
    import json, tempfile, os
    d = tempfile.mkdtemp(prefix="saikai-ctx-")
    p = os.path.join(d, "s.jsonl")
    recs = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000,
                      "cache_creation_input_tokens": 200, "output_tokens": 50}}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 131, "cache_read_input_tokens": 715734,
                      "cache_creation_input_tokens": 4017, "output_tokens": 4229}}},
    ]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    # last usage block: 131 + 715734 + 4017
    assert saikai._ctx_tokens_from_jsonl(p) == 719882
    # no usage anywhere -> None
    p2 = os.path.join(d, "n.jsonl")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    assert saikai._ctx_tokens_from_jsonl(p2) is None
    # missing file -> None (never raises)
    assert saikai._ctx_tokens_from_jsonl(os.path.join(d, "nope.jsonl")) is None
```

- [ ] **Step 2: Run it, verify it fails** — `uv run python tests/test_resource_bounds.py` -> `AttributeError: module 'saikai' has no attribute '_ctx_tokens_from_jsonl'`.

- [ ] **Step 3: Implement** (add after `_ram_per_pane_mb`, ~saikai.py:2917)

```python
def _ctx_tokens_from_jsonl(path) -> "int | None":
    """Live context size of a session, read from the LAST transcript record that
    carries a usage block: input + cache_read + cache_creation input tokens (the
    number `/context` shows). Ground truth, no estimation. None if the file is
    unreadable or has no usage yet. Reads only the tail (transcripts are large)."""
    try:
        import os
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 400_000))      # tail: the last usage is near the end
            chunk = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    last = None
    for ln in chunk.splitlines():
        ln = ln.strip()
        if not ln.startswith("{") or '"usage"' not in ln:
            continue
        try:
            msg = (json.loads(ln).get("message") or {})
            u = msg.get("usage") if isinstance(msg, dict) else None
        except Exception:
            continue
        if isinstance(u, dict) and "input_tokens" in u:
            last = u
    if last is None:
        return None
    return (int(last.get("input_tokens", 0))
            + int(last.get("cache_read_input_tokens", 0))
            + int(last.get("cache_creation_input_tokens", 0)))
```

- [ ] **Step 4: Run it, verify PASS** — `uv run python tests/test_resource_bounds.py` -> `PASS test_ctx_tokens_reads_last_usage_block`.

- [ ] **Step 5: Commit**

```bash
git add saikai.py tests/test_resource_bounds.py
git commit -F <msg-file>   # feat(ctx): read ground-truth live-context tokens from the transcript usage block
```

---

## Task 2 (a): window inference (from observed tokens, not the model string)

**Files:** Modify `saikai.py` (new pure fn after Task 1's). Test: `tests/test_resource_bounds.py`.

- [ ] **Step 1: Write the failing test**

```python
def test_ctx_window_inferred_from_observed_tokens():
    # message.model lacks the [1m] suffix, so infer the tier from the count.
    assert saikai._ctx_window_for(96_000) == 200_000
    assert saikai._ctx_window_for(200_000) == 200_000
    assert saikai._ctx_window_for(719_882) == 1_000_000     # this repo's real session
    assert saikai._ctx_window_for(1_200_000) == 1_000_000   # clamp to top tier
    assert saikai._ctx_window_for(50_000, override=500_000) == 500_000
```

- [ ] **Step 2: Run, verify fail** — `AttributeError: ... '_ctx_window_for'`.

- [ ] **Step 3: Implement**

```python
_CTX_TIERS = (200_000, 1_000_000)

def _ctx_window_for(tokens, override=None) -> int:
    """Context window for a session. The transcript's message.model is the base id
    (no `[1m]`), so the window can't be read from it -- infer the smallest tier that
    fits the observed count (a 720K reading can't be a 200K window). env/config
    override wins."""
    if override:
        return int(override)
    for t in _CTX_TIERS:
        if tokens <= t:
            return t
    return _CTX_TIERS[-1]
```

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `feat(ctx): infer the context window tier from observed tokens`.

---

## Task 3 (a): gauge formatter (text + severity colour)

**Files:** Modify `saikai.py` (new pure fn). Test: `tests/test_resource_bounds.py`.

- [ ] **Step 1: Write the failing test**

```python
def test_ctx_gauge_segment_formats_and_colours():
    # None tokens -> empty (no usage yet / unreadable).
    assert saikai._ctx_gauge_segment(None, 200_000) == ""
    # healthy: green, K-rounded, percent.
    s = saikai._ctx_gauge_segment(96_000, 200_000)
    assert "ctx 96K/200K (48%)" in s and "[green]" in s
    # 1M window, heavy: 719882/1.0M = 72% -> red (>= high band 70).
    s2 = saikai._ctx_gauge_segment(719_882, 1_000_000)
    assert "720K/1.0M (72%)" in s2 and "[red]" in s2
    # warn band (>= 55, < 70) -> yellow.
    s3 = saikai._ctx_gauge_segment(120_000, 200_000)   # 60%
    assert "[yellow]" in s3
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** (reuse the band shape from `_load_severity`/`_LOAD_COL`)

```python
def _ctx_severity(pct) -> str:
    if pct >= 0.70:
        return "crit"
    if pct >= 0.55:
        return "warn"
    return "ok"

def _fmt_k(n) -> str:
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n/1000)}K"

def _ctx_gauge_segment(tokens, window) -> str:
    """Statusbar 'ctx' segment for the focused pane: ground-truth fill, K-rounded,
    severity-coloured (green<55% / yellow / red>=70%). '' when tokens is None."""
    if tokens is None or not window:
        return ""
    pct = tokens / window
    col = _LOAD_COL[_ctx_severity(pct)]
    return f"[{col}]ctx {_fmt_k(tokens)}/{_fmt_k(window)} ({pct*100:.0f}%)[/{col}]"
```

- [ ] **Step 4: Run, verify PASS.** (`_fmt_k(719_882)` -> `720K`; `_fmt_k(1_000_000)` -> `1.0M`.)
- [ ] **Step 5: Commit** — `feat(ctx): severity-coloured context-fill gauge formatter`.

---

## Task 4 (a): wire the gauge into the statusbar for the focused pane

**Files:** Modify `saikai.py` (the statusbar builder + `_restat_live` cache). Test: `tests/test_keyboard_leader.py` (Pilot).

- [ ] **Step 1: Write the failing Pilot test** (append + register in `__main__`)

```python
def test_pilot_ctx_gauge_in_statusbar():
    """A focused live pane shows a ctx gauge in the statusbar from the transcript's
    usage block. (Stubs a live pane + sid_index entry; no real claude.)"""
    try:
        from textual.app import App  # noqa: F401
    except Exception:
        print("SKIP test_pilot_ctx_gauge_in_statusbar (textual unavailable)"); return
    import asyncio, json, uuid
    from textual.app import App
    sid = str(uuid.uuid4())
    pdir = _FAKE_HOME / ".claude" / "projects" / "-ctx-demo"
    pdir.mkdir(parents=True, exist_ok=True)
    jp = pdir / f"{sid}.jsonl"
    jp.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8",
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 95_000,
                  "cache_creation_input_tokens": 900, "output_tokens": 10}}}) + "\n",
        encoding="utf-8")
    facts = {}
    def fake_run(self, *a, **kw):
        async def go():
            async with self.run_test(size=(140, 30)) as pilot:
                await pilot.pause(0.3)
                facts["seg"] = saikai._ctx_gauge_segment(
                    saikai._ctx_tokens_from_jsonl(str(jp)),
                    saikai._ctx_window_for(saikai._ctx_tokens_from_jsonl(str(jp))))
        asyncio.run(go())
    orig, App.run = App.run, fake_run
    try:
        sys.argv = ["saikai", "--all"]; saikai.main()
    finally:
        App.run = orig
    # 96000/200000 = 48% -> the segment renders the gauge.
    assert "ctx 96K/200K (48%)" in facts.get("seg", ""), facts
```

(This test pins the formatter end-to-end on a real transcript read; the statusbar-append wiring below is verified by manual run + the existing statusbar Pilot staying green.)

- [ ] **Step 2: Run, verify fail** (until Task 1-3 are in; if 1-3 already committed this passes — then add the wiring in Step 3 and assert the live statusbar contains "ctx" via a focused-pane stub).

- [ ] **Step 3: Implement the statusbar wiring.** Read the statusbar builder (the method that ends with `self.query_one("#statusbar", Static).update(text)`). Inside the `if self._live is not None:` block, after `live_str` is built, append the focused pane's gauge:

```python
            # Context-fill gauge for the FOCUSED live pane (ground-truth tokens).
            _ft = self._focused_terminal()
            if _ft is not None:
                _jp = (self._sid_index.get(getattr(_ft, "sid", None)) or {}).get("jsonl_path")
                if _jp:
                    _tok = _ctx_tokens_from_jsonl(_jp)
                    if _tok is not None:
                        _seg = _ctx_gauge_segment(_tok, _ctx_window_for(
                            _tok, override=_cfg("context", "window", "SAIKAI_CTX_WINDOW", 0, int) or None))
                        if _seg:
                            live_str += f"{sep}{_seg}"
```

(Use the same `sep` the builder already uses; `_cfg(...)` mirrors the existing config-read helper — verify its signature at the existing `_ram_gate_kwargs` call site and match it; pass `override=None` if `_cfg` returns 0/empty.)

- [ ] **Step 4: Run** `uv run python tests/test_keyboard_leader.py` -> the new test PASS + all existing PASS. Manually run saikai with a live pane to eyeball `ctx N/200K (X%)` in the statusbar.
- [ ] **Step 5: Commit** — `feat(ctx): show the focused pane's context-fill gauge in the statusbar`.

---

## Task 5 (c): lineage recovery-pointer sidecar

**Files:** Modify `saikai.py` (clone the custom-titles sidecar). Test: `tests/test_resource_bounds.py`.

- [ ] **Step 1: Write the failing test**

```python
def test_lineage_sidecar_roundtrip():
    # _set_lineage(child, parent, parent_jsonl) persists; _load_lineage reads it back.
    saikai._set_lineage("child-sid", "parent-sid", "/path/parent.jsonl")
    lin = saikai._load_lineage()
    assert lin["child-sid"]["parent"] == "parent-sid"
    assert lin["child-sid"]["parent_jsonl"] == "/path/parent.jsonl"
    assert "ts" in lin["child-sid"]
```

(Runs against the test's `_FAKE_HOME` `CACHE_DIR` — `tests/test_resource_bounds.py` must set the throwaway home BEFORE importing saikai, mirroring `test_keyboard_leader.py:18-25`. If it does not yet, add that preamble.)

- [ ] **Step 2: Run, verify fail** — `AttributeError: ... '_set_lineage'`.

- [ ] **Step 3: Implement** (next to `CUSTOM_TITLES_FILE` and `_load_custom_titles`/`_set_custom_title`; clone their mtime-cache + atomic-write exactly)

```python
LINEAGE_FILE = CACHE_DIR / "lineage.json"     # child_sid -> {parent, parent_jsonl, ts}
_LINEAGE_CACHE: "dict | None" = None
_LINEAGE_MTIME: "float | None" = None

def _load_lineage() -> dict:
    global _LINEAGE_CACHE, _LINEAGE_MTIME
    try:
        m = LINEAGE_FILE.stat().st_mtime
    except OSError:
        _LINEAGE_CACHE, _LINEAGE_MTIME = {}, None
        return {}
    if _LINEAGE_CACHE is not None and m == _LINEAGE_MTIME:
        return _LINEAGE_CACHE
    raw = _read_json(LINEAGE_FILE, {})
    _LINEAGE_CACHE = raw if isinstance(raw, dict) else {}
    _LINEAGE_MTIME = m
    return _LINEAGE_CACHE

def _set_lineage(child: str, parent: str, parent_jsonl: str) -> None:
    global _LINEAGE_CACHE, _LINEAGE_MTIME
    import time
    d = dict(_load_lineage())
    d[child] = {"parent": parent, "parent_jsonl": parent_jsonl,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_json(LINEAGE_FILE, d)
    _LINEAGE_CACHE, _LINEAGE_MTIME = None, None     # force reload next read
```

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `feat(lineage): recovery-pointer sidecar (child -> parent)`.

---

## Task 6 (c): "open parent" action + binding

**Files:** Modify `saikai.py` (`action_open_parent` + a `BINDINGS` entry + `_MODAL_BLOCKED_ACTIONS`). Test: `tests/test_keyboard_leader.py` (Pilot).

- [ ] **Step 1: Write the failing test** (Pilot: write two demo sessions, set lineage child->parent, focus the child row, run `open_parent`, assert the cursor lands on the parent sid). Use `_write_demo_session()` twice; set `saikai._set_lineage(child, parent, parent_jsonl)`; press the bound key (or `await pilot.app.run_action("open_parent")`); assert `self._cursor_sid() == parent`.

- [ ] **Step 2: Run, verify fail** — no `action_open_parent`.

- [ ] **Step 3: Implement.** Add to `BINDINGS` (near the other priority bindings):

```python
            Binding("shift+f6", "open_parent", "Parent", id="open_parent", show=False, priority=True),
```

Add `"open_parent"` to `_MODAL_BLOCKED_ACTIONS`. Add the method:

```python
        def action_open_parent(self) -> None:
            """Jump to the session this one was forked/cleared from (lineage
            recovery). No-op + toast when there is no recorded parent."""
            sid = self._cursor_sid()
            rec = _load_lineage().get(sid or "")
            parent = rec.get("parent") if rec else None
            if not parent or parent not in self._sid_index:
                self.notify("no parent session recorded", timeout=3); return
            try:
                self._move_cursor_to_sid(parent)   # use the existing cursor-move helper
            except Exception:
                self.notify("could not open parent", severity="error", timeout=4)
```

(Verify the real cursor-move helper name — search for how `_cursor_sid`/`move_cursor` is used in the table; reuse that exact call. If none exists, move via `self.query_one("#table", DataTable).move_cursor(row=<row of parent>)` resolving the row from the rendered order.)

- [ ] **Step 4: Run, verify PASS** + full `test_keyboard_leader.py` green.
- [ ] **Step 5: Commit** — `feat(lineage): Shift+F6 "open parent" jumps to the forked-from session`.

---

## Task 7 (b1): AgentTerminal.paste_text + submit helpers

**Files:** Modify `saikai_terminal.py` (new methods next to `on_paste`, ~905). Test: `tests/test_terminal_concurrency.py` or a small new unit (stub `_pty`).

- [ ] **Step 1: Write the failing test** (stub a fake `_pty` recording writes; assert `paste_text` wraps in bracketed-paste when `_bracketed_paste` is True, raw when False; `submit` writes `\r`).

```python
def test_paste_text_wraps_and_submits():
    import saikai_terminal as st
    t = st.AgentTerminal.__new__(st.AgentTerminal)
    writes = []
    t._pty = type("P", (), {"write": lambda self, d: writes.append(d)})()
    t.is_dead = False
    t._bracketed_paste = True
    t.paste_text("/handoff")
    assert writes == ["\x1b[200~/handoff\x1b[201~"]
    writes.clear(); t._bracketed_paste = False
    t.paste_text("/compact")
    assert writes == ["/compact"]
    writes.clear(); t.submit()
    assert writes == ["\r"]
    # dead pane: no write
    writes.clear(); t.is_dead = True
    t.paste_text("x"); t.submit()
    assert writes == []
```

- [ ] **Step 2: Run, verify fail** — no `paste_text`.

- [ ] **Step 3: Implement** (mirror `on_paste`'s body, ~saikai_terminal.py:905)

```python
    def paste_text(self, text: str) -> None:
        """Inject text into the pane as a PASTE (bracketed when claude enabled
        ?2004h) so embedded newlines don't submit line-by-line. UI-thread only."""
        if self._pty is None or self.is_dead or not text:
            return
        if getattr(self, "_bracketed_paste", False):
            text = "\x1b[200~" + text + "\x1b[201~"
        try:
            self._pty.write(text)
        except Exception:
            pass

    def submit(self) -> None:
        """Send a single Enter (\\r) to submit the current input. UI-thread only."""
        if self._pty is None or self.is_dead:
            return
        try:
            self._pty.write("\r")
        except Exception:
            pass
```

- [ ] **Step 4: Run, verify PASS.** Then `uv run python tests/test_terminal_concurrency.py` + `tests/test_pty_backend.py` green.
- [ ] **Step 5: Commit** — `feat(term): paste_text/submit helpers for orchestrated input`.

---

## Task 8 (b1): action_context_refresh = inject /compact (idle-gated)

**Files:** Modify `saikai.py` (`action_context_refresh` + Shift+F11 binding + `_MODAL_BLOCKED_ACTIONS`). Test: `tests/test_keyboard_leader.py` (Pilot, stub pane).

- [ ] **Step 1: Write the failing Pilot test.** Stub a focused live pane exposing `paste_text`/`submit` (record calls) + a status. Assert: when the pane is idle, `action_context_refresh` calls `paste_text("/compact")` then `submit()`; when the pane is busy, it does NOT inject and toasts.

- [ ] **Step 2: Run, verify fail** — no `action_context_refresh`.

- [ ] **Step 3: Implement.** Add to `BINDINGS`:

```python
            Binding("shift+f11", "context_refresh", "Refresh ctx", id="ctx_refresh", show=False, priority=True),
```

Add `"context_refresh"` to `_MODAL_BLOCKED_ACTIONS`. Add the method (b1 = compact only; b2 wired in Task 11):

```python
        def action_context_refresh(self) -> None:
            """Shift+F11 on a focused live pane: inject /compact to summarise in
            place (non-destructive). No-op + toast when no pane is focused or the
            pane is mid-turn (don't interrupt a running turn)."""
            t = self._focused_terminal()
            if t is None:
                self.notify("focus a live pane to refresh its context", timeout=3); return
            sid = getattr(t, "sid", None)
            status = self._live.statuses().get(sid) if self._live else None
            if status in ("busy", "waiting"):     # mid-turn / awaiting input -> don't inject
                self.notify("pane is busy — refresh when it's idle", severity="warning", timeout=3); return
            t.paste_text("/compact")
            t.submit()
            self.notify("sent /compact to compact this session in place", timeout=4)
```

(Verify the real busy/streaming status value from `self._live.statuses()` and adjust the guard set; the statusbar already reads `_st = self._live.statuses()` so reuse that vocabulary.)

- [ ] **Step 4: Run, verify PASS** + full suites green (incl. `test_terminal_concurrency.py`).
- [ ] **Step 5: Commit** — `feat(ctx): Shift+F11 injects /compact (idle-gated), non-destructive`.

---

## Task 9 (b1): mirror key-bar "Refresh ctx" button

**Files:** Modify `saikai_mirror.py` (secondary key-bar row). Test: `tests/test_mirror_input.py`.

- [ ] **Step 1: Write the failing test** — add to `test_page_key_bar_has_saikai_action_keys` the assert `'data-k="shift+f11"' in page`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — add `'<button data-k="shift+f11">Refresh</button>'+` to the `#kb2` innerHTML (next to the other action buttons).
- [ ] **Step 4: Run** `uv run python tests/test_mirror_input.py` -> OK (incl. the no-control-byte guard).
- [ ] **Step 5: Commit** — `feat(mirror): Refresh-ctx (shift+f11) button on the key bar`.

---

## Task 10 (SPIKE — task zero of b2): verify /clear mints a detectable new sid

**Files:** none (a verification spike). Output: a short note appended to the spec's "Open items".

- [ ] **Step 1:** In a REAL saikai live pane, note the current sid (from `_sid_index` / the pane title). Type `/clear`. Watch `~/.claude/projects/<enc>/` for a NEW `*.jsonl`.
- [ ] **Step 2:** Record: (i) does `/clear` create a new sid + new transcript file (vs reuse the same)? (ii) how long until the new transcript appears? (iii) does its first record's `cwd` match the pane? (iv) are sibling/`claude -p` transcripts a real contamination risk in that dir during the window?
- [ ] **Step 3:** Write the findings into the spec. **DECISION GATE:** if `/clear` does NOT mint a detectable new sid, STOP — b2's `/clear`-in-place + child detection is not viable; either (alt) launch a fresh saikai-controlled pane seeded with the handoff (saikai owns the new sid), or ship b1 only. Do not build b2 until this is answered.
- [ ] **Step 4: Commit** the spec note — `docs(spec): record /clear new-sid spike findings`.

---

## Task 11 (b2): human-gated checkpoint -> /handoff -> confirm -> /clear -> reseed  [ONLY IF SPIKE POSITIVE]

**Files:** Modify `saikai.py` (extend `action_context_refresh` with a b2 path behind a confirm modal + a tick state machine; record lineage). Test: `tests/test_keyboard_leader.py` (Pilot with a stub pane + a mocked transcript handoff).

Build this as a **tick-driven state machine** (off `_poll_live_status` or a self-cancelling `set_interval(0.3)`), NEVER a blocking wait. States: `inject_handoff -> await_handoff_idle -> extract_prompt -> confirm(modal) -> inject_clear -> detect_child -> inject_reseed -> record_lineage`. Each tick advances at most one step; the destructive `/clear` is sent ONLY after the user confirms in the modal.

- [ ] **Step 1: Write the failing tests** (pure + Pilot):
  - A pure sequence/asserts: a helper returns the ordered step names and the test asserts `inject_clear` comes AFTER `confirm` and AFTER `await_handoff_idle`; and that the reseed step references the parent handoff/prompt.
  - Pilot: stub pane records `paste_text`/`submit`; mock the transcript so the "handoff" assistant turn contains a `NEW SESSION PROMPT` fenced block; drive the machine; assert NO `/clear` is injected before the confirm modal is dismissed with Enter; after confirm, `/clear` then the reseed prompt are injected; `_load_lineage()` records child->parent.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** the state machine + a `ConfirmRefreshScreen(ModalScreen)` that shows the extracted `NEW SESSION PROMPT` (Enter=proceed / Esc=cancel). Extract the prompt by reading the pane's transcript (`_ctx`-style tail read) for the last assistant message and slicing the fenced `NEW SESSION PROMPT` block. Detect the child sid per the spike's proven method (capture pre-existing sids; bind the first new sid whose first-record cwd matches + ts post-dates clear; on 0 or >=2, toast + record nothing). Record `_set_lineage(child, parent, parent_jsonl)`. Trigger b2 from a distinct gesture (e.g. Shift+F11 held / a second key) so b1's plain `/compact` stays the default.
- [ ] **Step 4: Run, verify PASS** + ALL suites green, especially `test_terminal_concurrency.py` + `test_pty_backend.py` (live-pane + timing surface).
- [ ] **Step 5: Commit** — `feat(ctx): human-gated checkpoint/clear/rehydrate with lineage`.

---

## Self-review (run before execution)

- **Spec coverage:** (a) gauge = Tasks 1-4; (c) recovery pointer = Tasks 5-6; (b1) /compact = Tasks 7-9; spike = Task 10; (b2) = Task 11. Deferred items (auto-nudge, lineage tree, bespoke handoff file) are correctly absent. ✓
- **No placeholders:** every code step shows real code or a precise anchor + the exact lines to add. The two "verify the exact name" notes (the cursor-move helper in Task 6; the busy-status value in Task 8) are grounded reads against named call sites, not vague TODOs. ✓
- **Type/name consistency:** `_ctx_tokens_from_jsonl` / `_ctx_window_for` / `_ctx_gauge_segment` / `_load_lineage` / `_set_lineage` / `action_open_parent` / `action_context_refresh` / `paste_text` / `submit` are used consistently across tasks. ✓
- **Concurrency:** all reads are UI-thread/parse-time; the only orchestration (Tasks 8, 11) writes to the PTY on the UI thread; b2 is explicitly tick-based (no blocking wait). `test_terminal_concurrency.py` is mandated after live-pane tasks. ✓
- **Ordering safety:** the destructive piece (b2) is LAST and gated on the Task-10 spike + a human confirm. b1 (/compact) is non-destructive and ships first. ✓
