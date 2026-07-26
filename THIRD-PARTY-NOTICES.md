# Third-party notices

saikai is licensed under the MIT License (see `LICENSE`). It depends on the
following third-party packages, installed separately via your package manager
(pip / uv) — saikai does **not** bundle or modify their source.

| Package | License | Role |
|---|---|---|
| [textual](https://github.com/Textualize/textual) | MIT | TUI framework |
| [rich](https://github.com/Textualize/rich) | MIT | text rendering (via textual) |
| [pyte](https://github.com/selectel/pyte) | **LGPL-3.0** | in-memory terminal emulator (split-live pane) |
| [pywinpty](https://github.com/andfoy/pywinpty) | MIT | Windows ConPTY backend (`sys_platform == 'win32'`) |
| [ptyprocess](https://github.com/pexpect/ptyprocess) | ISC | POSIX PTY backend (non-Windows) |

## Vendored terminal assets

The browser mirror bundles the following unmodified xterm.js distribution
assets under `saikai_mirror_static/`. They are served only to render the local
mirror; unlike the packages above, these files are copied into this repository.

| Vendored files | Upstream package | License | License file |
|---|---|---|---|
| `xterm.min.js`, `xterm.min.css` | [@xterm/xterm 5.5.0](https://www.npmjs.com/package/@xterm/xterm/v/5.5.0) | MIT | [upstream `LICENSE`](https://unpkg.com/@xterm/xterm@5.5.0/LICENSE) |
| `addon-canvas.js` | [@xterm/addon-canvas 0.7.0](https://www.npmjs.com/package/@xterm/addon-canvas/v/0.7.0) | MIT | [upstream `LICENSE`](https://unpkg.com/@xterm/addon-canvas@0.7.0/LICENSE) |

`addon-saikai-rich-graphemes.js` is saikai project code. It adapts the vendored
xterm Unicode API to the same grapheme-break and Rich cell-width tables used by
the local renderer.

## Note on pyte (LGPL-3.0)

saikai imports `pyte` as an ordinary, unmodified dependency installed by the user
(pip/uv). It is **not** copied into this repository or statically combined with
saikai's source. Under the LGPL-3.0 this "use as a separately-installed library"
case does not impose the LGPL on saikai's own code, and the dynamic Python import
satisfies the requirement that the library remain user-replaceable. saikai's own
source therefore remains under the MIT License.

If you redistribute saikai together with a copy of pyte (e.g. a vendored bundle
or a frozen binary), review the LGPL-3.0 terms — in that case you must keep pyte
under the LGPL and allow it to be replaced.
