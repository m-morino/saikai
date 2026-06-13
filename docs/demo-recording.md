# Demo recording

The public demo must never use a real work history or a private project.

The current hero GIF is a deterministic UI recording built from fictional
Claude transcript data and a scripted right-hand pane:

```bash
uv run scripts/make_demo_gif.py
```

This path is useful for repeatable UI checks. It is not evidence that a real
Claude Code process is running in the pane.

A real-Claude recording must be made in a disposable Linux environment with a
dedicated demo home, fictional repositories, and no inherited credentials,
hooks, MCP servers, plugins, or project instructions. Audit the recorded text
before replacing `docs/assets/saikai-demo.gif`.

The repository will add an automated fixture and audit command for that
workflow before publishing a real-Claude hero recording.
