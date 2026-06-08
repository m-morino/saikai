# Third-party notices

recap is licensed under the MIT License (see `LICENSE`). It depends on the
following third-party packages, installed separately via your package manager
(pip / uv) — recap does **not** bundle or modify their source.

| Package | License | Role |
|---|---|---|
| [textual](https://github.com/Textualize/textual) | MIT | TUI framework |
| [rich](https://github.com/Textualize/rich) | MIT | text rendering (via textual) |
| [pyte](https://github.com/selectel/pyte) | **LGPL-3.0** | in-memory terminal emulator (split-live pane) |
| [pywinpty](https://github.com/andfoy/pywinpty) | MIT | Windows ConPTY backend (`sys_platform == 'win32'`) |
| [ptyprocess](https://github.com/pexpect/ptyprocess) | ISC | POSIX PTY backend (non-Windows) |

## Note on pyte (LGPL-3.0)

recap imports `pyte` as an ordinary, unmodified dependency installed by the user
(pip/uv). It is **not** copied into this repository or statically combined with
recap's source. Under the LGPL-3.0 this "use as a separately-installed library"
case does not impose the LGPL on recap's own code, and the dynamic Python import
satisfies the requirement that the library remain user-replaceable. recap's own
source therefore remains under the MIT License.

If you redistribute recap together with a copy of pyte (e.g. a vendored bundle
or a frozen binary), review the LGPL-3.0 terms — in that case you must keep pyte
under the LGPL and allow it to be replaced.
