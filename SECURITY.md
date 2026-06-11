# Security Policy

## Supported versions

recap is pre-1.0; security fixes land on the latest `0.1.x` release.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/m-morino/recap/security/advisories/new):
go to the repository's **Security** tab → **Report a vulnerability**. That keeps
the report confidential until a fix is available and requires no email address.

When reporting, include:

- the recap version (`recap --version`) and your OS / terminal,
- steps to reproduce, and
- the impact you observed.

You can expect an initial acknowledgement within a few days. Because recap runs
a local `claude` subprocess in a PTY and reads your own `~/.claude` transcripts,
the most relevant classes of issue are local privilege / data-exposure bugs
(e.g. a transcript or prompt leaking somewhere it shouldn't, or unsafe handling
of untrusted transcript content). Reports in those areas are especially welcome.
