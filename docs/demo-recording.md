# Demo recording

The public demo must never use a real work history or private project.

There are two different artifacts:

- `docs/assets/saikai-demo-headless.gif` is a deterministic UI regression demo.
  It uses fictional transcripts and `scripts/mock_claude.py`; it is not
  evidence that a real Claude Code process is running.
- `docs/assets/saikai-demo.gif` is the public hero shown in the README. It is
  currently the same deterministic, leak-checked render as the regression GIF
  above — `scripts/mock_claude.py` faithfully reproduces the Claude Code UI, so
  the pane looks real without launching a real session (no auth, no token, no
  history to leak). For an even more authentic hero, replace it with an audited
  recording made with real Claude Code in the isolated environment below.

For a **full feature tour** (every selling point, including the web mirror) —
the long-form counterpart to the 25–35 s hero storyboard below — follow
[demo-script.md](demo-script.md). It uses the same isolation contract and audit.

Regenerate the deterministic assets with:

```bash
uv run scripts/make_screenshots.py
uv run scripts/make_demo_gif.py
```

## Isolation contract

Record only in a dedicated Linux VM, a dedicated WSL distro, or a disposable
Linux OS user named `demo`. The environment must have:

1. No real HOME, Claude history, project, browser profile, keychain, or shell
   history.
2. No host-drive mounts or SSH agent. For a recording-only WSL distro, disable
   Windows-drive automount in `/etc/wsl.conf`, restart that distro, and verify
   `/mnt/c` does not exist.
3. A recording-only Claude credential injected at runtime and removed
   immediately after recording.
4. Claude Code launched through the bare wrapper below. It forces `--bare`,
   ignores all other MCP configuration, and allows only `Read`, `Bash`, and
   `Edit`.
5. Cast audit before any GIF/MP4 conversion, followed by manual frame review.

Do not put a credential in this document, a command argument, or shell history.
Use a prompt that does not echo the value:

```bash
read -rsp "Recording-only ANTHROPIC_API_KEY: " ANTHROPIC_API_KEY
echo
export ANTHROPIC_API_KEY
unset SSH_AUTH_SOCK
```

## Prepare the fixture

Assume this repository is cloned at `/home/demo/src/saikai`:

```bash
export SAIKAI_REPO=/home/demo/src/saikai
cd "$SAIKAI_REPO"

python scripts/demo_fixture.py --root /home/demo/saikai-demo
export HOME=/home/demo/saikai-demo/home
export CLAUDE_CONFIG_DIR="$HOME/.claude"
export SAIKAI_SUMMARIZE_ENABLED=0
export SAIKAI_AUTO_REFRESH=0
```

Install saikai, then put a recording-only wrapper ahead of the real `claude`
binary. This makes split-live resumes use the same minimal Claude configuration
as the seed command:

```bash
uv tool install "$SAIKAI_REPO"
REAL_CLAUDE="$(command -v claude)"
mkdir -p /home/demo/bin
cat > /home/demo/bin/claude <<EOF
#!/bin/sh
exec "$REAL_CLAUDE" --bare --strict-mcp-config \
  --mcp-config '{"mcpServers":{}}' --allowedTools Read Bash Edit "\$@"
EOF
chmod 700 /home/demo/bin/claude
export PATH=/home/demo/bin:$PATH
export SAIKAI_DEMO_BARE_WRAPPER=1
```

Seed one real resumable conversation in the fictional repository:

```bash
cd /home/demo/saikai-demo/repos/webapp
claude -p \
  "Inspect the failing auth test and explain the likely cause. Do not edit files."
```

The fixture also contains fictional background sessions so the first frame
shows a realistic cross-project history. It never reads or copies the caller's
real HOME or Claude history.

## Record and audit

Start the recorder through the safety-checking helper:

```bash
cd "$SAIKAI_REPO"
python scripts/record_demo.py --record-real \
  --root /home/demo/saikai-demo \
  --cast /home/demo/saikai-demo/saikai-real.cast
```

The helper refuses to record unless the OS user is `demo`, HOME and
`CLAUDE_CONFIG_DIR` point at the fixture, `/mnt/c` and `SSH_AUTH_SOCK` are
absent, a recording-only credential is present, and the bare wrapper is marked
active. It audits the cast immediately after recording.

Run the audit again before conversion:

```bash
python scripts/audit_demo_cast.py /home/demo/saikai-demo/saikai-real.cast
```

The auditor rejects Windows/WSL host paths, non-demo Linux homes, unapproved
fictional project names, API keys, bearer tokens, private-key headers,
localhost auth URLs, and email addresses. Add local deny regexes as newline
separated values in `SAIKAI_DEMO_DENY`.

Only after the audit passes:

```bash
agg /home/demo/saikai-demo/saikai-real.cast \
  "$SAIKAI_REPO/docs/assets/saikai-demo.gif" \
  --theme monokai --speed 1.25 --cols 128 --rows 35
```

## Hero storyboard

Keep the hero to ≈30 s. Establish **what saikai is** in the first few seconds
(so the standout has context), then land the one standout — the safe, one-key
reset of a bloated session. One theme, two beats; do not tour every feature.

**Beat 1 — identity (≈0–10 s): mission control for your Claude Code fleet.**

1. Open on many sessions across projects (`saikai --all`), with a couple already
   running as live panes. Do not begin with help or a menu.
2. Flip between two live panes (`F3`/`F2`); show the status markers `~` working /
   `?` waiting / `!` finished-needs-you, grouped/sorted by status.
3. Next-attention (`Shift+F3`) jumps straight to the pane that needs you.
   Caption: **"Every Claude Code session, across every repo — live, grouped by
   what needs you."**

**Beat 2 — the standout (≈10–28 s): spot the bloated one, reset it safely in one key.**

4. Focus a pane whose statusbar gauge is **red** — e.g. `ctx 712K/1.0M (71%)`.
   Caption: **"Real context fill per pane — straight from the transcript. This one's
   bloated and getting dumber."**
5. *(optional, cut if tight)* `Shift+F11` injects `/compact`; the gauge drops and
   recolours toward green. Caption: **"Shift+F11 = one-key /compact."**
6. Leader `Space` then `c` (Checkpoint). Show the row's `↻` marker, then the
   confirm modal with the extracted `NEW SESSION PROMPT`. **Hold here — the trust
   beat.** Caption: **"It shows you the new prompt before anything clears. You decide."**
7. Press `Enter`: `/clear` runs, the pane reseeds lean, the gauge goes green.
   Caption: **"Enter → /clear → fresh, lean session, auto-seeded."**
8. Press `Shift+F6`: the parent (pre-clear) session is still there.
   Caption: **"Shift+F6 → back to the old session if you need it. Nothing lost."**
9. End on: **See what's bloated. Reset it in one key. Resume lean — safely.**

Money-shot still (for the social/OG card): the split view with the status-grouped
list on the left **and** a red-gauge pane with the Checkpoint modal open over it —
identity + standout in one frame.

**Recording prerequisites for Beat 2 (note for whoever records):** the fixture must
seed a **high-context** session so the gauge reads red, and the pane must be able to
run the Checkpoint flow (handoff turn → `NEW SESSION PROMPT` → `/clear` → reseed).
`scripts/mock_claude.py` does not reproduce that flow today, so Beat 2 needs **real
Claude Code in the isolated environment** (or a `mock_claude.py` extension that emits
a fenced `NEW SESSION PROMPT` and honours `/clear`). Beat 1 records as before.

Do not spend hero time on favorites, the command menu, Settings, the web mirror,
or every key.

## Secondary clips

Keep these separate from the hero:

- **Attention loop:** two live sessions; one finishes and one waits; jump only
  between panes that need the human.
- **Working-set restore:** quit saikai, relaunch, and restore the previous pane
  set. Emphasize that no daemon is required.
- **History as work record:** show transcript-derived changes, related sessions,
  and opening-prompt reuse.

## Manual review

After conversion, inspect every frame at full size. Confirm:

- only `/home/demo` paths and the fictional repositories appear;
- the pane is visibly real Claude Code, not `mock_claude.py`;
- no credential, email, host mount, notification, browser chrome, or private
  terminal title appears;
- the text remains readable at GitHub README width;
- the final public GIF tells the storyboard above without unexplained pauses.

Remove the recording-only credential and destroy the disposable environment
after the final audited artifact is copied out.
