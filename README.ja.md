# saikai

[![CI](https://github.com/m-morino/saikai/actions/workflows/ci.yml/badge.svg)](https://github.com/m-morino/saikai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/saikai)](https://pypi.org/project/saikai/)
[![GitHub release](https://img.shields.io/github/v/release/m-morino/saikai)](https://github.com/m-morino/saikai/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | **日本語**

Claude Code は会話を残してくれます。ただ、リポジトリや worktree が増えるほど、
目的のセッションをもう一度見つけるのが難しくなります。再開候補が現在の作業
ディレクトリを起点に見えるためです。

Claude Desktop のように会話を見渡せる体験を、デスクトップアプリを開き続けず
Linux ターミナルの中で使いたくて、saikai を作りました。過去のセッションを
横断検索し、元の作業ディレクトリから再開し、いま人間の入力を待っている
セッションを見分けるための道具です。

**見つける。再開する。次に見るべきものがわかる。**

名前は「**再開**」と「**再会**」から取りました。

![saikai demo](docs/assets/saikai-demo.gif)

<sub>デモは漏洩チェック済みの架空データです。現在の GIF は再現可能な UI
録画です。公開録画の隔離・監査方法は
[録画ガイド](docs/demo-recording.md)に記載しています。</sub>

### 一覧の読み方

**色は文脈、記号は状態を表します。** 既定では同じプロジェクトのタイトルが
同じ色になります。狭い split 表示でプロジェクト列が隠れても、関連する作業を
見分けるためです。`display.color_by` を `worktree` / `topic` / `none` にすると
色分けの基準を変えられます。

`~` 作業中 · `?` 入力待ち · `!` 完了（未回答） · `=` ライブ・確認済み ·
`@` 別で開いている · `+` アクティブ · `.` 最近 · `*` お気に入り · `x` 非表示

## 何が変わるか

- **cwd をまたいで見つける。** リポジトリや worktree を横断して Claude Code
  の履歴を検索し、そのセッションが始まった cwd から再開できます。
- **人間の入力が必要な場所がわかる。** 複数の本物の `claude` を split-live
  タブで動かし、`~` 作業中 / `?` 入力待ち / `!` 完了を見分けられます。
- **作業中の一式を戻せる。** 開いていたペインの組み合わせを終了時に記録し、
  後から `Shift+F4` で戻せます。
- **タイトルの先まで確認できる。** トランスクリプトのプレビュー、変更差分、
  プロンプト再利用、推定した親子セッションの追跡ができます。
- **ローカルの履歴をそのまま使う。** Claude 自身のトランスクリプトを読み取り
  専用で参照し、デーモンもデータベースも追加しません。AI 要約は任意です。

実装は [Textual](https://github.com/Textualize/textual) 上の小さな Python
モジュール 3 つです。ライブペインを増やす前に警告する RAM ゲートもあります。

> ライブペインは Windows では ConPTY、それ以外では POSIX PTY を使います。
> OS ごとの検証状況は[プラットフォーム対応](#プラットフォーム対応)を参照
> （現時点で検証済みプラットフォームは Windows です）。

## インストール

**Python ≥ 3.11** が必要です。いちばん簡単なのは
[uv](https://docs.astral.sh/uv/) — PyPI パッケージを隔離環境へ導入するため、
venv の手作業は不要です:

```bash
uv tool install saikai   # 安定版 → `saikai` が PATH に
```

クローンから:

```bash
uv run saikai.py          # その場で実行（依存は自動インストール）
uv tool install .        # `saikai` コマンドを PATH に入れて: saikai
```

pip / pipx 派でも大丈夫です（依存は `pyproject.toml` から）:

```bash
pipx install saikai   # 隔離環境 + PATH
pip install saikai   # アクティブな環境へ
```

split-live ペインには PTY 系の依存（`pyte`、Windows は `pywinpty` / それ以外は
`ptyprocess`）が必要ですが、上記のどのコマンドでも自動で入ります。万一欠けて
いても、saikai は一覧専用モードで動き続けます（後述）。

## 使い方

```bash
saikai                 # 全プロジェクト・全履歴（これが既定）
saikai --here          # 現在のプロジェクト（git リポジトリ）のみ
saikai --days 7        # 直近 7 日のみ（ワンショット。--save-defaults で永続化）
saikai --table         # 静的な一覧（非対話）
saikai --help
```

### キー操作 — 覚えるのは3つだけ

残りは画面が教えてくれます（フッター、ドロップダウン、迷ったときに出る `␣`
メニュー）。

1. **すでに知っているキー。** `↑` `↓` 移動 · `Enter` 再開 · `/` か文字を打てば
   検索 · `Tab` プレビュー切替 · `?` キー一覧 · `Esc` は「今の文脈から抜ける」
   （検索 → 一覧 → 終了）。
2. **`Space` がメニュー。** 一覧で `Space`、続けて 1 文字。`Space` の後に手が
   止まると、系統別の全メニューがその場に表示されます（which-key 方式）—
   暗記は不要:

   | セッション操作 | 表示 | ペイン |
   |---|---|---|
   | `f` ★ お気に入り | `s` ソート列を切替 (**s**ort) | `n` 新規セッション |
   | `h` 非表示 | `o` ソート方向を反転 (**o**rder) | `p` ペイン復元 |
   | `e` 名前変更 (edit) | `g` グループ切替 | `z` ペインをフリーズ |
   | `y` プロンプトをコピー (yank) | `t` ツリー | `a` 次の要対応ペインへ |
   | `d` 変更内容 (diff) | `l` 一覧の表示/非表示 | `x` タブを閉じる · `[` `]` タブ移動 |
   | `r` 再読込 | `,` 設定 · `/` バー表示切替 | `Space` 一括起動用マーク |

3. **`Ctrl+]` でペインから一覧へ**フォーカスを戻します（ペイン内の他のキーは
   すべて claude のもの — 中の claude は普通に使えます）。

**検索トークン**（テキストや他のトークンと組み合わせ可）: `:fav` `:hidden`
`:open` `:active` `:recent`。フィルタバー（検索ボックス + Group / Sort /
Status / Age）は既定で表示: `Tab`/`Shift+Tab` でドロップダウンに入り `Enter`
で開く。`␣/` でバーを畳んで行数を稼げます（選択は記憶）。

そのほか: `Alt+←/→` で一覧/ペインの境界を移動（ドラッグ同様に保存）。メニューの
各操作にはレガシーな F キー別名もあり、`?` がリマップ込みで全一覧を表示します。

既定が好みに合わなければ `config.toml` で: `[keys] leader = "none"` でモードを
無効化（Space は従来どおり直接マーク）、`leader = "ctrl+g"` で別キーへ、
`leader_defaults = false` でマップを空に、`action = "x"` の 1 文字指定で個別の
リマップができます。**マウスでの加点操作**（必須ではありません）: 列ヘッダの
クリックでソート、境界のドラッグ、行やドロップダウンのクリック。

### split-live（既定）

![split-live: 左にセッション一覧、右にライブな claude ペイン](docs/assets/saikai-split-live.svg)

PTY 依存（`pyte`、`pywinpty`/`ptyprocess`）が揃っていれば、saikai は本物の
対話型 `claude` プロセスを一覧の隣のタブで動かします — 依存として同梱される
ため、これが既定です。軽量な一覧専用ブラウザ（`Enter` = 全画面での再開）に
切り替えるには環境変数を設定します:

```bash
SAIKAI_SPLIT_LIVE=0 saikai     # false / no / off も可
```

| キー | 動作 |
|-----|--------|
| `Enter` | 選択中セッションのライブペインを開く / フォーカス |
| `Shift+F8` | 任意のフォルダ / git worktree で新規 claude セッションを開始 |
| `Shift+F4` | 前回開いていたペイン一式を再展開（スナップショット + 再開） — いつでも |
| `F2` / `F3` | 前 / 次のライブタブ |
| `Shift+F3` | 次の要対応ペインへジャンプ（`?` 入力待ち / `!` 完了） |
| `F4` | セッション一覧の表示 / 非表示（ペインを全幅に） |
| `Ctrl+]` | ペイン → 一覧へフォーカスを戻す（`SAIKAI_RELEASE_KEY` で変更可） |
| `F10` / `Shift+F10` | アクティブなタブを閉じる / 全タブを閉じる（明示クローズ — 復元対象外） |
| `Esc` / `Ctrl+C` | 終了: 開いているペインをスナップショットして全終了（次回 `Shift+F4` で再展開） |
| 上スクロール | ペインをフリーズ（コピーモード）: claude が動いたまま選択・コピー |

saikai 内で `?` を押すと、現在の色分け基準と記号の凡例を確認できます。

## 設定（環境変数）

| 変数 | 既定値 | 意味 |
|---|---|---|
| `SAIKAI_SPLIT_LIVE` | on | ライブペインモード。`0`/`false`/`no`/`off` で無効化 → 一覧専用 + 全画面再開 |
| `SAIKAI_AUTO_REFRESH` | off | バックグラウンド再スキャンの間隔（秒） |
| `SAIKAI_SUMMARIZE_CMD` | — | `claude -p` の代わりに使うサマリ生成コマンド（stdin にプロンプト → stdout にサマリ） |
| `SAIKAI_AUTO_PERMISSION` | off | 常用ワークスペースで `--permission-mode auto` を付ける動作を明示的に有効化 |
| `SAIKAI_MAX_MEM_LOAD` | 85 | このメモリ負荷 % を超えたらペインを開くのを拒否/警告（Win は `dwMemoryLoad`、Linux/macOS は導出値） |
| `SAIKAI_MIN_COMMIT_MB` | 2048 | **コミット余裕**をこれだけ確保 — システムフリーズ対策（Win/Linux） |
| `SAIKAI_MIN_FREE_PHYS_PCT` | 8 | 物理 RAM の空きをこの % 以上確保（スラッシング防止フロア、マシン相対） |
| `SAIKAI_CLAUDE_MB` | 600 | ライブペイン 1 つあたりの推定 RAM |
| `SAIKAI_MIN_FREE_MB` | 0 | 任意の絶対物理フロア（レガシー。% フロアとの最大値を採用） |
| `SAIKAI_HARD_RAM_GATE` | off | `1` でゲート超過時に警告ではなく拒否 |
| `SAIKAI_MAX_LIVE` | 64 | 同時ライブペイン数の上限（保険） |
| `SAIKAI_SCROLLBACK` | 2000 | ペインごとにメモリへ保持するスクロールバック行数。**ライブプロセスの RAM への最大の効きどころ**（満杯のペイン ≈ 列×行の pyte セル）。メモリが厳しいマシンでは下げ（例 1000）、履歴を深くしたければ上げる |
| `SAIKAI_COLOR_BY` | project | セッションタイトルの色分け基準: `project` / `worktree` / `topic` / `none` |
| `SAIKAI_SPLIT_RATIO` | 0.34 | 一覧/ペインの初期分割比（境界のドラッグや `Alt+←/→` で変更でき、その値が保存される） |
| `SAIKAI_RELEASE_KEY` | `ctrl+]` | ライブペインから一覧へフォーカスを戻すキー |

同じ項目は任意の **TOML 設定ファイル**でも指定できます（優先順位は
`環境変数 > 設定ファイル > 既定値`）。`[keys]` でのキー再割当も可能です。
`saikai --init-config` でコメント付きテンプレートを生成、`saikai
--print-config` で解決済みの場所と値を確認できます。アプリ内では
**`Space ,`** で Settings 画面が開き、リスト系オプションをその場で変更しつつ、
全設定の解決値と出所を一覧できます（`e` で config.toml をエディタで開く）。

現在統合済みのproviderはClaude Codeです。agent固有の起動・状態contractは
`saikai_provider.py`へ分離済みで、Codex contractも抽象化検証用にありますが、
履歴発見とlive状態連携が完成するまでCodexは選択可能にしません。Claudeの
履歴探索は`CLAUDE_CONFIG_DIR`にも対応します。

## 周辺ツールとの関係

saikai は、時間をまたいでセッションを見つけ、再開し、状況を把握することに
重点を置いています。[ccmanager](https://github.com/kbwo/ccmanager) は、動作中の
複数 agent をまとめて調整するためのツールです。同じ問題を競合して解くのでは
なく、隣り合う用途です。

## プラットフォーム対応

**検証済みの正式対応はWindows 10 / 11、Python ≥ 3.11に限定します。**
Linux、WSL2、macOSの実装は維持しますが、各OSの実PTY経路で継続的な実機検証が
揃うまではexperimental扱いです。それ以外のplatformは非対応です。

saikai 本体は純粋な Python + Textual です。プラットフォーム固有なのは
**split-live ペイン**だけ（実 PTY とクリップボードを扱うため）。正直な現状:

| OS | ライブペイン PTY | クリップボード（フリーズしたペインから） | RAM ゲートの情報源 | 状態 |
|---|---|---|---|---|
| **Windows** 10 / 11 | ConPTY (`pywinpty`) | Win32 `CF_UNICODETEXT`（コードページ安全） | `GlobalMemoryStatusEx` | ✅ **開発・常用環境**（WezTerm 上） |
| **Linux** *（WSL2 含む）* | POSIX PTY (`ptyprocess`) | OSC-52 *（terminal / tmux / SSH側の許可が必要）* | `/proc/meminfo` | ⚠️ **experimental・実機レビュアー募集** |
| **macOS** | POSIX PTY (`ptyprocess`) | localは`pbcopy`、remote対応terminalではOSC-52 fallback | `sysctl` + `vm_stat` *（負荷 + 物理のみ。コミット制限なし）* | ⚠️ **experimental・macOSレビュアー募集** |

- **Windows ではターミナル非依存:** クリップボードは Win32 `CF_UNICODETEXT`
  API、PTY は ConPTY を通るため、どちらもホストターミナルに依存しません —
  **Windows Terminal** も検証済みの WezTerm と同じコードパスを通るので、同様に
  動作するはずです。
- **一覧専用モード**（`SAIKAI_SPLIT_LIVE=0`）は PTY 依存がなく、Textual が
  動くところならどこでも動くはずです。
- ヘッドレス回帰テストはプラットフォーム非依存です（textual / pyte / pywinpty
  をスタブ化）。素の `python` だけで実行できます（依存不要）:

  ```bash
  python tests/test_config.py
  python tests/test_providers.py
  python tests/test_sort_recency.py
  python tests/test_split_divider.py
  python tests/test_terminal_concurrency.py
  python tests/test_resource_bounds.py
  python tests/test_terminal_watchdog.py
  python tests/test_keyboard_leader.py
  python tests/test_pty_backend.py
  ```

- Linux / WSL2 / macOSのレビュアーを募集しています。terminal名、localか
  SSH/tmux経由か、PTY終了、キー入力、clipboardの結果をissueで共有してください。
- CIではWindows、Linux、macOSそれぞれの実PTY backendをinstallし、spawn、
  resize、出力、EOFをsmoke testします。ただしterminal emulator、SSH、tmux、
  IME、clipboard policyを組み合わせた対話実機レビューの代替にはなりません。

## コントリビュート

Issue / PR を歓迎します — 開発環境の準備、テストの実行方法、split-live ペインを
デッドロックさせないための**並行処理の不変条件**（スレッド周りを触る前に必読）
は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。変更履歴は
[CHANGELOG.md](CHANGELOG.md) に記録しています。

## セキュリティ

脆弱性は非公開で報告してください — [SECURITY.md](SECURITY.md) を参照。
セキュリティ問題を公開 issue にしないようお願いします。

## ライセンス

saikai は [MIT License](LICENSE) で公開しています。別途インストールされる
サードパーティパッケージ（textual、pyte、pywinpty/ptyprocess）に依存します —
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) を参照。なお `pyte` は
LGPL-3.0 ですが、無改変・別インストールの依存として利用しているため、saikai
自身のコードは MIT のままです。
