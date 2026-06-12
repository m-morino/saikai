# Launch copy — ready-to-paste drafts

## Show HN (Hacker News)

**Title** (≤ 80 chars):
```
Show HN: saikai – keyboard-first terminal browser for your Claude Code history
```

**Body** (paste into the text field):
```
I use Claude Code heavily across several git repos and worktrees, and after a
few weeks I had dozens of sessions I could no longer navigate. `claude --resume`
works great but you have to already know the session ID.

saikai opens a full-screen Textual TUI that scans ~/.claude/projects and shows
every conversation — searchable, grouped by date / project / topic, with one-line
AI titles. Pressing Enter resumes the session right there in split panes (the
default "split-live" mode), so you can keep several conversations open at once
and jump between them without losing scrollback.

It's complementary to ccmanager (managing active multi-agent rosters); saikai is
for navigating and resuming the history.

Tech: pure Python + Textual, ConPTY on Windows / POSIX PTY elsewhere, no daemon
or database, MIT-licensed. Keyboard-first — Space is a leader key, Alt+←/→
resizes the split; the mouse is a bonus, never a requirement.

GitHub: https://github.com/m-morino/saikai
```

**Timing**: Monday or Tuesday, 9–11 AM US Eastern (= 22:00–midnight JST).

---

## Reddit

### r/ClaudeAI

**Title**:
```
saikai: browse and resume every Claude Code session from a keyboard-first TUI
```

**Body**:
```
If you use Claude Code across multiple projects you probably have dozens of
sessions you can't easily find. I built saikai to fix that.

It scans ~/.claude/projects, shows every conversation in a searchable table
(filter as you type, group by date/project/topic), and resumes any session in
one keypress. The default mode runs them live in split panes so you can have
several conversations open at once.

Keyboard-first by design — Space is a leader key with mnemonics (Space f =
favourite, Space s = cycle sort, etc.), everything has a keybind, mouse is optional.

https://github.com/m-morino/saikai
MIT licensed, Python + Textual, works on Windows/Linux/macOS.
```

### r/commandline

Same body. Title variant:
```
saikai: a Textual TUI for browsing / resuming Claude Code sessions (split-live PTY, keyboard-first)
```

---

## Twitter / X

**With GIF attached**:
```
Built a keyboard-first terminal browser for your Claude Code history.

Search, group by date/project, resume in one keypress — or run sessions
live side-by-side without closing anything.

github.com/m-morino/saikai
MIT · Python · @TextualizeHQ TUI

#ClaudeCode #terminal #opensource
```

**Thread / follow-up tweet**:
```
The Space key is a leader — Space f = favourite, Space s = cycle sort,
Space h = hide, etc. Alt+←/→ resizes the split. Mouse is a bonus, not
a requirement.

Full key map at ?: [screenshot]
```

---

## Zenn (日本語記事)

**タイトル案**:
```
Claude Code のセッションが増えすぎた人へ — saikai でターミナルから全部管理する
```

**本文の軸**:
1. 課題：セッションが増えて `claude --resume` だけでは追えなくなる
2. saikai とは何か（スクショ/GIF）
3. インストール（1 コマンド）と基本操作
4. キーボードファーストの設計思想（Space リーダー、Alt+←/→）
5. ccmanager との使い分け
6. Windows で開発・日常利用しているという個人的な経緯

---

## Product Hunt

**Tagline** (60 chars):
```
Browse & resume every Claude Code session from a TUI
```

**Description**:
```
saikai is a keyboard-first terminal session browser for Claude Code. It scans
your ~/.claude/projects history, shows every conversation in a searchable,
sortable, groupable table, and resumes any session in one keypress.

The default "split-live" mode runs sessions live in tabs beside the list using
a real PTY (ConPTY on Windows, POSIX PTY elsewhere) — jump between conversations
without losing scrollback. Space is a leader key with 20 mnemonic mappings;
Alt+←/→ resizes the split. Everything works without a mouse.

Complements ccmanager (live multi-agent management); saikai handles the history.
```

**Topics**: Developer Tools, Terminal, Productivity, Open Source, CLI

---

## Ecosystem positioning note

When posting anywhere, mention the ccmanager relationship:
- "complements ccmanager (1.1k ★) for live agent management"
- "if you use ccmanager you might also want saikai for the history side"

This borrows discovery from a project with existing traction without misrepresenting overlap.
