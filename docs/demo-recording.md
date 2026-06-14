# Demo recording

The public demo must never use a real work history or private project.

There are two different artifacts:

- `docs/assets/saikai-demo-headless.gif` is a deterministic UI regression demo.
  It uses fictional transcripts and `scripts/mock_claude.py`; it is not
  evidence that a real Claude Code process is running.
- `docs/assets/saikai-demo.gif` is the public hero. Replace it only with an
  audited recording made with real Claude Code in the isolated environment
  below.

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

Keep the hero to 25-35 seconds. It should make one claim, not tour every
feature:

1. Open on many sessions across projects. Do not begin with help or a menu.
2. Search for a remembered phrase from the target conversation.
3. Pause briefly on the cross-project result and its project/title color.
4. Press Enter. Real Claude Code resumes in the correct fictional cwd.
5. Ask Claude to fix the failing test and run the suite; show genuine tool
   activity.
6. Return to the list as a background pane changes to `!` or `?`.
7. Use next-attention to jump directly to it.
8. End on: **Find it. Resume it. Know what needs you.**

Do not spend hero time on favorites, the command menu, Settings, or every key.

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
