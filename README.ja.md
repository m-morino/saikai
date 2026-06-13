# saikai

[![CI](https://github.com/m-morino/saikai/actions/workflows/ci.yml/badge.svg)](https://github.com/m-morino/saikai/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/saikai)](https://pypi.org/project/saikai/)
[![GitHub release](https://img.shields.io/github/v/release/m-morino/saikai)](https://github.com/m-morino/saikai/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | **日本語**

Claude Code のセッションが複数のリポジトリや worktree に増えると、目的の
セッションを探しづらくなります。`claude --resume` の候補が、現在の作業
ディレクトリを起点に表示されるためです。

saikai は、Claude Code の履歴をまとめて検索し、元の作業ディレクトリから
再開するための TUI です。複数の `claude` を右ペインで動かし、作業中・入力待ち・
処理完了の状態も一覧で確認できます。Claude Desktop のような一覧画面を、
普段使うターミナル内でも使いたくて作りました。

```bash
uv tool install saikai
saikai
```

![saikai demo](docs/assets/saikai-demo.gif)

<sub>デモは漏洩チェック済みの架空データです。現在の GIF は再現可能な UI
録画です。公開録画の隔離・監査方法は
[録画ガイド](docs/demo-recording.md)に記載しています。</sub>

### 一覧の読み方

タイトルの色は、既定ではプロジェクトごとに分かれます。狭い split 表示で
プロジェクト列が隠れても、同じプロジェクトの作業を見分けられます。
`display.color_by` を `worktree` / `topic` / `none` にすると基準を変えられます。
行頭の記号はセッションの状態です。

`~` 作業中 · `?` 入力待ち · `!` 処理完了・要応答 · `=` 待機中・応答不要 ·
`@` 別で開いている · `+` アクティブ · `.` 最近 · `*` お気に入り · `x` 非表示

## 主な機能

- リポジトリや worktree を横断して履歴を検索し、セッション開始時の cwd から
  再開できます。
- 複数の `claude` を split-live タブで動かし、作業中・入力待ち・処理完了を
  一覧で確認できます。
- 終了時に開いていたペインを記録し、後から `Shift+F4` で開き直せます。
- トランスクリプトのプレビュー、変更差分、プロンプト再利用、推定した親子
  セッションの追跡ができます。
- Claude 自身のトランスクリプトは読み取り専用で参照します。デーモンや
  データベースは追加せず、AI 要約も任意です。

## こんな人に向いています

自分が欲しかったのは、複数の Claude Code セッションを一画面で管理し、その場で
行き来できる TUI セッションマネージャーでした。特に次のような使い方に合います。

- リポジトリや worktree ごとに増えたセッションを、元の cwd を覚えていなくても
  横断検索して再開する。
- 複数の `claude` を並行で動かし、どれが作業中・入力待ち・処理完了なのかを
  一覧で確認する。
- ターミナルを閉じた後、開いていたセッション一式を戻す。
- Claude Desktop のような一覧画面を、常駐するデスクトップアプリではなく
  普段使うターミナル内で使う。
- タイトルだけでは見つからない過去の作業を、会話内容、変更差分、以前の
  プロンプトから探す。

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
saikai                 # 全プロジェクト・全履歴（初期状態。保存済み設定があればそちらを使用）
saikai --here          # 現在のプロジェクト（git リポジトリ）のみ
saikai --days 7        # 直近 7 日のみ（ワンショット。--save-defaults で永続化）
saikai --table         # 静的な一覧（非対話）
saikai --help
```

### キー操作 — 覚えるのは3つだけ

残りは画面が教えてくれます（フッター、ドロップダウン、迷ったときに出る `␣`
メニュー）。

1. **すでに知っているキー。** `↑` `↓` 移動 · `Enter` 再開 · `/` か文字を打てば
   検索 · `Tab` プレビュー切替 · `?` キー一覧 · saikai 側では `Esc` で「今の
   文脈から抜ける」（検索 → 一覧 → 終了）。ライブペインから一覧へ戻るには
   `Ctrl+]` を使います。
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

3. **`Ctrl+]` でペインから一覧へ**フォーカスを戻します。通常の編集キーは
   claude に届き、saikai の F キー操作も引き続き使えます。

**検索トークン**（テキストや他のトークンと組み合わせ可）: `:fav` `:hidden`
`:open` `:active` `:recent`。フィルタバー（検索ボックス + Group / Sort /
Status / Age）は既定で表示: `Tab`/`Shift+Tab` でドロップダウンに入り `Enter`
で開く。`␣/` でバーを畳んで行数を稼げます（選択は記憶）。

そのほか: `Alt+←/→` で一覧/ペインの境界を移動（ドラッグ同様に保存）。主な
セッション・ペイン操作には F キー別名もあり、`?` で利用可能な別名とリマップを
確認できます。

既定が好みに合わなければ `config.toml` で: `[keys] leader = "none"` でモードを
無効化（Space は従来どおり直接マーク）、`leader = "ctrl+g"` で別キーへ、
`leader_defaults = false` でマップを空に、`action = "x"` の 1 文字指定で個別の
リマップができます。マウスでは列ヘッダのソート、境界のドラッグ、行や
ドロップダウンのクリックに加え、ライブペイン内の任意範囲をドラッグしてコピー
できます。セッション管理の主な操作はキーボードだけでも行えます。

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
| 一覧で `Esc` | 開いているペインをスナップショットして全終了（次回 `Shift+F4` で再展開） |
| `Ctrl+C` | ライブペインでは claude を中断。saikai 側の操作領域では全終了 |
| 上スクロール | ペインをフリーズ（コピーモード）: claude が動いたまま選択・コピー |

saikai 内で `?` を押すと、現在の色分け基準と記号の凡例を確認できます。

## 設定（環境変数）

| 変数 | 既定値 | 意味 |
|---|---|---|
| `SAIKAI_SPLIT_LIVE` | on | ライブペインモード。`0`/`false`/`no`/`off` で無効化 → 一覧専用 + 全画面再開 |
| `SAIKAI_AUTO_REFRESH` | off | バックグラウンド再スキャンの間隔（秒）。`0` は無効、最小有効値は `2` |
| `SAIKAI_SUMMARIZE_ENABLED` | off | `claude -p` による AI 要約を有効化 |
| `SAIKAI_SUMMARIZE_CMD` | — | `claude -p` の代わりに使うサマリ生成コマンド（stdin にプロンプト → stdout にサマリ） |
| `SAIKAI_SUMMARIZE_MODEL` | haiku | `claude -p` で要約するときのモデル |
| `SAIKAI_AUTO_PERMISSION` | off | 常用ワークスペースで `--permission-mode auto` を付ける動作を明示的に有効化 |
| `SAIKAI_MAX_MEM_LOAD` | 85 Win / 95 POSIX | このメモリ負荷 % を超えたらペインを開くのを拒否/警告。Windows の `dwMemoryLoad` は独立したカーネル信号。Linux/macOS の負荷はフロアと同じ可用量から導出する重複信号なので、既定を高くして保険に格下げ |
| `SAIKAI_MAX_MEM_PRESSURE` | 10 | Linux/macOS: 実測のメモリ**圧迫**がこの値を超えたら新規ペインを拒否（Linux は PSI `some avg10` % — systemd-oomd が判定に使う停止時間メトリクス。macOS はカーネルの critical 圧迫レベルで発動）。Windows では無効 |
| `SAIKAI_MIN_COMMIT_MB` | 2048 | **コミット余裕**をこれだけ確保 — システムフリーズ対策。Windows は常時。Linux は**厳格 overcommit**（`vm.overcommit_memory=2`）のときだけ — 既定のヒューリスティックモードでは CommitLimit は強制されないためスキップ |
| `SAIKAI_MIN_FREE_PHYS_PCT` | 8 | 物理 RAM の空きをこの % 以上確保（スラッシング防止フロア、マシン相対） |
| `SAIKAI_CLAUDE_MB` | 600 | ライブペイン 1 つあたりの推定 RAM |
| `SAIKAI_MIN_FREE_MB` | 0 | 任意の絶対物理フロア（レガシー。% フロアとの最大値を採用） |
| `SAIKAI_HARD_RAM_GATE` | off | `1` でゲート超過時に警告ではなく拒否 |
| `SAIKAI_MAX_LIVE` | 64 | 同時ライブペイン数の上限（保険） |
| `SAIKAI_SCROLLBACK` | 2000 | ペインごとに saikai がメモリへ保持するスクロールバック行数。pyte のセル数に効くため、メモリが厳しいマシンでは下げ（例 1000）、履歴を深くしたければ上げる |
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

saikai 本体の大部分は Python + Textual ですが、実 PTY、クリップボード、
プロセス終了、キー入力などの split-live 周辺は OS やホストターミナルの影響を
受けます。現状:

| OS | ライブペイン PTY | クリップボード（フリーズしたペインから） | RAM ゲートの情報源 | 状態 |
|---|---|---|---|---|
| **Windows** 10 / 11 | ConPTY (`pywinpty`) | Win32 `CF_UNICODETEXT`（コードページ安全） | `GlobalMemoryStatusEx` | ✅ **開発・常用環境**（WezTerm 上） |
| **Linux** *（WSL2 含む）* | POSIX PTY (`ptyprocess`) | OSC-52 *（terminal / tmux / SSH側の許可が必要）* | `/proc/meminfo` + PSI + overcommit mode | ⚠️ **experimental・実機レビュアー募集** |
| **macOS** | POSIX PTY (`ptyprocess`) | localは`pbcopy`、remote対応terminalではOSC-52 fallback | `vm_stat` + memory-pressure sysctl *（コミット制限なし）* | ⚠️ **experimental・macOSレビュアー募集** |

- Windows の PTY とクリップボードは、WezTerm と Windows Terminal で同じ
  ConPTY / Win32 API 経路を通ります。ただし、キー入力、描画幅、IME、マウス操作
  まで同一とは限りません。日常利用で確認しているのは WezTerm です。
- **一覧専用モード**（`SAIKAI_SPLIT_LIVE=0`）では PTY とライブペインの
  クリップボード経路を使わないため、最も移植性の高い使い方です。
- 多くの回帰テストは依存がなくても実行できますが、Textual Pilot と実 PTY
  backend の経路まで確認するには依存を入れて `uv run` で実行します:

  ```bash
  uv run python tests/test_config.py
  uv run python tests/test_demo_audit.py
  uv run python tests/test_demo_fixture.py
  uv run python tests/test_keyboard_leader.py
  uv run python tests/test_providers.py
  uv run python tests/test_pty_backend.py
  uv run python tests/test_resource_bounds.py
  uv run python tests/test_sort_recency.py
  uv run python tests/test_split_divider.py
  uv run python tests/test_terminal_concurrency.py
  uv run python tests/test_terminal_watchdog.py
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
