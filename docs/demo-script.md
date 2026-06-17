# saikai full demo script (real Claude Code)

A **comprehensive feature tour** — the counterpart to the 25–35 s hero in
[demo-recording.md](demo-recording.md). The hero makes one claim; this script
shows every selling point with **real Claude Code** running against the
**fictional, leak-checked fixture**. Record it the same isolated, audited way as
the hero.

> **Read [demo-recording.md](demo-recording.md) first.** Its **Isolation
> contract** (disposable `demo` OS user, no real HOME/history/keychain, a
> recording-only credential injected at runtime, the bare-`claude` wrapper, and
> the cast audit) applies to **every** segment here. Never record from a real
> workstation or real HOME.

The tour has two segments because they capture different surfaces:

- **Segment A — the TUI**, recorded as an asciinema cast with
  `scripts/record_demo.py --record-real` (terminal only).
- **Segment B — the web mirror**, a **screen recording of a browser/phone** beside
  the terminal (asciinema cannot capture the browser).

Each scene below lists the **action** (keys/taps), the **caption/voiceover**, and
**what must be on screen**. Keep the whole tour ≈ 2–3 min; cut any scene rather
than rush. The fixture's projects are `webapp`, `api-server`, `data-pipeline`,
`dotfiles` (see `scripts/demo_fixture.py`); the seeded resumable conversation is
**webapp → "Fix flaky auth token refresh test"**.

---

## Segment A — the TUI (asciinema)

Prepare + start the recorder exactly as in demo-recording.md:

```bash
python scripts/record_demo.py --record-real --root /home/demo/saikai-demo \
  --cast /home/demo/saikai-demo/saikai-tour.cast
```

### A1 — Find across projects (the core problem)
- **Action:** open on the full list (`saikai --all`); do not start on help/a menu.
- **Show:** many sessions across `webapp` / `api-server` / `data-pipeline` /
  `dotfiles`, each project a distinct title color; the marker legend
  (`~ ? ! = @ + . * x`).
- **Caption:** *"Every Claude Code session, across every repo — not just this cwd."*

### A2 — Search by what you remember
- **Action:** type `auth` (search-as-you-type opens the filter bar); pause on the
  cross-project hit **"Fix flaky auth token refresh test"**.
- **Show:** the list narrowing live; the project/title color of the result.
- **Caption:** *"Find it by content, not by remembering where it lived."*

### A3 — Resume it as a live pane
- **Action:** `Enter` on the result.
- **Show:** real Claude Code resuming in the correct fictional cwd
  (`/home/demo/work/webapp`) in a split-live pane beside the list.
- **Caption:** *"Resume in the original directory — as a live pane, not a takeover."*

### A4 — Drive Claude; watch the markers
- **Action:** ask Claude to *"run the focused auth test and explain the failure"*;
  show genuine tool activity. While it works, `Ctrl+]` back to the list.
- **Show:** the pane's marker move `~` (working) → `!` (finished / awaiting reply);
  another seeded session sitting at `?` (waiting).
- **Caption:** *"See what's working, what's waiting, what needs you — at a glance."*

### A5 — Jump to what needs you
- **Action:** `Shift+F3` (or `Space` then `a`) — next-attention.
- **Show:** focus jumping straight to the `?`/`!` pane, skipping idle ones.
- **Caption:** *"Jump only to the sessions that actually need a human."*

### A5b — See which session is bloated (the context gauge)
- **Action:** focus a pane that has accumulated a lot of context (seed a
  high-context session in the fixture for this); read its statusbar gauge.
- **Show:** the gauge in **red**, e.g. `ctx 712K/1.0M (71%)`; a leaner pane reads
  green. `/context` is per-session — saikai shows every pane's fill at once.
- **Caption:** *"Real context fill per pane, from the transcript — see which
  session is bloated and getting dumber."*

### A5c — One-key /compact (the everyday stay-lean)
- **Action:** on the bloated pane, `Shift+F11`.
- **Show:** `/compact` injected in place; the gauge drops and recolours toward green.
- **Caption:** *"Shift+F11 = /compact in place — the everyday way to stay lean."*

### A5d — Checkpoint: reset on purpose, safely (the standout)
- **Action:** leader `Space` then `c`. Let the handoff turn run (the row shows
  `↻`); the confirm modal appears with the extracted `NEW SESSION PROMPT`. **Pause
  on it.** Press `Enter`.
- **Show:** saikai never types `/clear` until your Enter; then `/clear` runs and the
  pane reseeds a fresh, lean session (gauge → green).
- **Caption:** *"Checkpoint: it writes a handoff, shows you the reseed prompt, and
  only on your Enter clears + restarts lean. It never /clears on its own."*

### A5e — Jump back to the parent (recovery)
- **Action:** `Shift+F6`.
- **Show:** the cursor moves to / opens the pre-clear parent session — still intact.
- **Caption:** *"Missed a detail in the lean handoff? Shift+F6 → back to the old session."*

> **Recording note for A5b–A5e:** the fixture needs a high-context seeded session so
> the gauge reads red, and the pane must run the real Checkpoint flow (handoff →
> `NEW SESSION PROMPT` → `/clear` → reseed). `scripts/mock_claude.py` doesn't
> reproduce that flow today, so record this cluster with **real Claude Code** in the
> isolated environment (or extend the mock to emit a fenced `NEW SESSION PROMPT` and
> honour `/clear`).

### A6 — The command menu (which-key)
- **Action:** press `Space` and **pause** ~1 s so the grouped menu appears; then
  `t` (tree) to show inferred parent/child chains, then `Space d` (diff) on a
  session to show transcript-derived changes.
- **Show:** the Session/View/Panes menu; the tree; the change view.
- **Caption:** *"Space is the menu — nothing to memorize. History, changes,
  prompt reuse, all two keystrokes."*

### A7 — Start something new, anywhere
- **Action:** `Shift+F8`, pick the `data-pipeline` folder, start a new session.
- **Caption:** *"New session in any repo or worktree — without leaving saikai."*

### A8 — Quit and restore the working set
- **Action:** `Esc` (quit — snapshots open panes), relaunch `saikai`, then
  `Shift+F4`.
- **Show:** the previous panes reopening (snapshot + resume). No daemon ran in
  between.
- **Caption:** *"Close the terminal; reopen the same working set later. No daemon,
  no database — it reads Claude's own transcripts."*

End Segment A on the restored multi-pane view.

---

## Segment B — the web mirror (browser / phone screen recording)

Record the **host terminal and a phone (or a second browser window) side by
side**. Keep the host on the isolated fixture; the QR/URL carry only the
disposable env's token (still, frame so the token text isn't dwelt on).

Launch with the mirror on, loopback for a same-machine demo or a LAN IP for a
real phone:

```bash
SAIKAI_MIRROR=1 saikai --all                                  # same machine
SAIKAI_MIRROR=1 SAIKAI_MIRROR_HOST=<demo-lan-ip> SAIKAI_MIRROR_ALLOW_LAN_INPUT=1 saikai --all
```

### B1 — Mirror to the phone
- **Action:** on the host, the QR is shown on launch (or press `F12`); scan it
  with the phone.
- **Show:** the phone browser rendering the **same** saikai UI live; the banner
  reads **`CONTROL OFF (read-only)`**.
- **Caption:** *"Mirror the live UI to your phone — opt-in, token-authenticated,
  read-only by default."*

### B2 — Turn on control (locally only)
- **Action:** on the **host**, press `Shift+F12`.
- **Show:** the phone banner flips to **`CONTROL ON`**; the host status bar shows
  the **`🌐 1`** connected-browser count.
- **Caption:** *"Control is off by default and can only be armed at the terminal —
  a browser can never enable itself. The terminal always shows who's connected."*

### B3 — Drive saikai by touch
- **Action:** on the phone, **tap** a session row (selects it), **tap a column
  header** (sorts), **swipe** to scroll the list.
- **Show:** the host UI responding to each touch.
- **Caption:** *"Tap to select and sort, swipe to scroll — straight into saikai's
  own UI."*

### B4 — Open and operate Claude from the phone
- **Action:** tap a row, then the on-screen **⏎ Enter** key to resume it into a
  pane; type a prompt with the phone keyboard; submit.
- **Show:** Claude responding in the pane, mirrored to the phone.
- **Caption:** *"Open a session and talk to Claude from the couch — full
  terminal-equivalent keys: the d-pad, Esc/Tab, Ctrl+C to interrupt."*

### B5 — Leave the pane, idle-disable
- **Action:** tap **☰ List** (or `Ctrl+]`) to return to the list; then leave it
  untouched to show control **auto-disabling after the idle window** (banner →
  `CONTROL OFF`).
- **Caption:** *"Step back to the list with one tap; control auto-disables when
  you walk away."*

End Segment B on the phone showing the live list with `CONTROL OFF`.

---

## Audit & assembly

- Audit the Segment A cast (`scripts/audit_demo_cast.py`) and review every frame —
  same as the hero (only `/home/demo` paths + fictional repos; no credential,
  email, host mount, or private title).
- For Segment B, review the screen recording frame-by-frame too: no real LAN
  hostname/IP beyond the disposable env, no token dwelt on, no real notification
  or browser chrome (bookmarks, other tabs, profile name).
- Assemble: Segment A (cast → GIF/MP4 via `agg`) then Segment B (the browser/phone
  clip). Keep captions short; let the genuine tool/Claude activity carry it.

## What this tour must prove (selling points → scenes)

| Selling point | Scene |
|---|---|
| Find sessions across repos/worktrees | A1, A2 |
| Resume from the original cwd | A3 |
| Several real `claude` in split-live; `~ ? !` at a glance | A3, A4 |
| Jump to what needs attention | A5 |
| Real per-pane context gauge (which session is bloated) | A5b |
| One-key /compact (stay lean) | A5c |
| Checkpoint: human-gated /clear + reseed lean (the standout) | A5d |
| Jump back to the parent after a checkpoint (recovery) | A5e |
| Command menu (which-key), history, diff, tree, prompt reuse | A6 |
| New session in any folder/worktree | A7 |
| Quit + restore the working set; no daemon/database | A8 |
| Mirror the UI to a phone (opt-in, token auth, read-only) | B1 |
| Local-only control toggle + connected-count visibility | B2 |
| Touch: tap/sort/swipe into saikai's own UI | B3 |
| Operate Claude from the phone (terminal-equivalent keys) | B4 |
| Release focus + idle auto-disable | B5 |
