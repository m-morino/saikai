# saikai

[![CI](https://github.com/m-morino/saikai/actions/workflows/ci.yml/badge.svg)](https://github.com/m-morino/saikai/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | **日本語**

[Claude Code](https://claude.com/claude-code) のターミナル用セッションブラウザです。
saikai は `~/.claude/projects` をスキャンし、過去のセッションを検索・ソート・
グループ化できる一覧表に、セッションごとの AI 生成 1 行サマリ付きで表示し、
どれでもその場で再開できます。既定の **split-live** モードでは、一覧の隣に
ライブな `claude` ペインをタブでホストし、複数セッションを並行して動かしながら
見守れます。名前は日本語の「**再開**」（そして過去のセッションとの「再会」）から。

![saikai のセッションブラウザ: 全 Claude Code セッションのソート可能な一覧とプレビューペイン](docs/assets/saikai-browse.svg)

<sub>スクリーンショットは架空のデモデータです — `uv run scripts/make_screenshots.py`
でいつでも再生成できます。</sub>

## ハイライト

- **全セッションをひとつの表に** — マシン上の全 Claude Code 会話を、実際の
  最終アクティビティ順に、日付 / プロジェクト / トピックでグループ化し、
  打鍵した先から絞り込み。
- **その場で再開** — `Enter` で `claude --resume` を、そのセッションが始まった
  作業ディレクトリから起動（git worktree 対応）。
- **split-live ペイン** — 一覧の隣のタブで複数のライブ `claude` セッションを
  ホスト。`~` 作業中 / `?` 入力待ち / `!` 完了（未読）がマーカーで一目瞭然。
  終了しても `Shift+F4` でペイン一式を丸ごと復元。
- **セッションの整理** — ★ お気に入り・非表示・名前変更、AI による 1 行
  タイトル、親子ツリー推定、大きな履歴向けの LLM トピッククラスタ。
- **RAM を意識** — メモリゲート（コミット余裕 + 負荷 + 物理フロア）が、
  新しいペインがマシンをスラッシングに追い込む前に警告。
- **キーボードファースト** — すべてマウスなしで操作可能。`Space` がリーダー
  キーで、ニーモニックな 1 文字が続きます（`Space f` = お気に入り、
  `Space s` = ソート…）。`Alt+←/→` で分割幅を変更、`?` で現在のキーマップを
  表示。マウスは加点要素（クリックでソート、ドラッグで分割）であって、
  必須ではありません。
- **押し付けない設計** — claude 自身のトランスクリプトを読み取り専用で参照。
  デーモンもデータベースもなし。[Textual](https://github.com/Textualize/textual)
  上の Python ファイル 2 つ、MIT ライセンス。

> ライブペインは Windows では ConPTY、それ以外では POSIX PTY を使います。
> OS ごとの検証状況は[プラットフォーム対応](#プラットフォーム対応)を参照
> （現時点で検証済みプラットフォームは Windows です）。

## インストール

**Python ≥ 3.11** が必要です。いちばん簡単なのは
[uv](https://docs.astral.sh/uv/) — インライン PEP-723 ヘッダーから依存を
解決するので、venv の手作業は不要です:

```bash
uv tool install git+https://github.com/m-morino/saikai   # クローン不要 → `saikai` が PATH に
```

クローンから:

```bash
uv run saikai.py          # その場で実行（依存は自動インストール）
uv tool install .        # `saikai` コマンドを PATH に入れて: saikai
```

pip / pipx 派でも大丈夫です（依存は `pyproject.toml` から）:

```bash
pipx install git+https://github.com/m-morino/saikai   # 隔離環境 + PATH
pip install .                                         # アクティブな環境へ
```

split-live ペインには PTY 系の依存（`pyte`、Windows は `pywinpty` / それ以外は
`ptyprocess`）が必要ですが、上記のどのコマンドでも自動で入ります。万一欠けて
いても、saikai は一覧専用モードで動き続けます（後述）。

## 使い方

```bash
saikai                 # 現在のプロジェクト（git リポジトリ）のセッション
saikai --all-projects  # ~/.claude/projects 配下の全プロジェクト
saikai --table         # 静的な一覧（非対話）
saikai --help
```

### キー操作

| キー | 動作 |
|-----|--------|
| `↑` `↓` / `Enter` | 移動 / 選択中のセッションを再開 |
| `/` または文字を打つだけ | 検索・フィルタバーを開く（`Esc` で閉じる。フィルタは維持） |
| `F5` | 再読込 · `F6` ★ お気に入り · `F7` 非表示 · `F8` 変更内容（diff） · `F9` 冒頭プロンプトをコピー |
| `Shift+F2` | セッション名の変更 — 自由に命名（空にすると自動タイトルへ戻る） |
| `Shift+F5/F6/F7` | ツリー / クラスタ / グループ切替 |
| `Tab` | プレビュー: 全文 ↔ サマリ · `?` ヘルプ · `Esc` 終了 |

**検索トークン**（テキストや他のトークンと組み合わせ可）: `:fav` `:hidden`
`:open` `:active` `:recent`。Group / Sort / Status / Age のドロップダウンも
あります。

### キーボードファースト: Space リーダー

saikai はマウスなしで完結します — マウスは加点要素であって必須ではありません。
一覧で **`Space`**（リーダーキー）を押し、続けてニーモニックな 1 文字を押すだけ
（最初の数回は全マップがヒント表示されます。`?` でも確認可能）:

| | | | |
|---|---|---|---|
| `f` ★ お気に入り | `h` 非表示 | `e` 名前変更 (edit) | `r` 再読込 |
| `d` 変更内容 (diff) | `y` プロンプトをコピー (yank) | `s` ソート列を切替 (**s**ort) | `o` ソート方向を反転 (**o**rder) |
| `g` グループ切替 | `t` ツリー | `c` クラスタ | `n` 新規セッション |
| `p` ペイン復元 | `z` ペインをフリーズ | `a` 次の要対応ペインへ | `l` 一覧の表示/非表示 |
| `x` タブを閉じる | `[` / `]` 前 / 次のタブ | `Space` 一括起動用マーク | |

リーダーは**セッション一覧にフォーカスがあるときだけ**発動します — ライブな
claude ペインや検索ボックスのキー入力を奪うことはありません。ほかのキーボード
操作:

- **分割幅の変更**: `Alt+←` / `Alt+→` で一覧/ペインの境界を動かせます
  （位置はドラッグ時と同様に保存されます）。
- **ドロップダウン**: `/` でフィルタバーを表示し、`Tab` / `Shift+Tab` で
  Group / Sort / Status / Age に入り、`Enter` で開きます。
- それ以外は**すべて** F キーがあり（上の表）、`?` がリマップ込みの現在の
  バインドを一覧表示します。

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

一覧のマーカー: `~` 作業中 · `?` 入力待ち · `!` 完了（未読） · `@` 表示中 ·
`+` アクティブ · `.` 最近 · `*` お気に入り · `x` 非表示。

## 設定（環境変数）

| 変数 | 既定値 | 意味 |
|---|---|---|
| `SAIKAI_SPLIT_LIVE` | on | ライブペインモード。`0`/`false`/`no`/`off` で無効化 → 一覧専用 + 全画面再開 |
| `SAIKAI_AUTO_REFRESH` | off | バックグラウンド再スキャンの間隔（秒） |
| `SAIKAI_SUMMARIZE_CMD` | — | `claude -p` の代わりに使うサマリ生成コマンド（stdin にプロンプト → stdout にサマリ） |
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

同じ項目は任意の **TOML 設定ファイル**でも指定できます（優先順位は
`環境変数 > 設定ファイル > 既定値`）。`[keys]` でのキー再割当も可能です。
`saikai --init-config` でコメント付きテンプレートを生成、`saikai
--print-config` で解決済みの場所と値を確認できます。

## プラットフォーム対応

**対応プラットフォーム（意図的に限定）: Windows、Linux（WSL を含む）、macOS、
Python ≥ 3.11。** それ以外は*非対応*です: 他の POSIX OS でも汎用 POSIX 経路で
動く可能性はあり、クラッシュせず安全に縮退しますが、未検証で対象外です。

saikai 本体は純粋な Python + Textual です。プラットフォーム固有なのは
**split-live ペイン**だけ（実 PTY とクリップボードを扱うため）。正直な現状:

| OS | ライブペイン PTY | クリップボード（フリーズしたペインから） | RAM ゲートの情報源 | 状態 |
|---|---|---|---|---|
| **Windows** 10 / 11 | ConPTY (`pywinpty`) | Win32 `CF_UNICODETEXT`（コードページ安全） | `GlobalMemoryStatusEx` | ✅ **開発・常用環境**（WezTerm 上） |
| **Linux** *（WSL 含む）* | POSIX PTY (`ptyprocess`) | OSC-52 *（OSC-52 対応ターミナルが必要）* | `/proc/meminfo` | ⚠️ コードは完成、**作者は未実走** |
| **macOS** | POSIX PTY (`ptyprocess`) | OSC-52 *（iTerm2 / kitty / WezTerm は OK。Terminal.app は要設定）* | `sysctl` + `vm_stat` *（負荷 + 物理のみ。コミット制限なし）* | ⚠️ コードは完成、**作者は未実走** |

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
  python tests/test_sort_recency.py
  python tests/test_split_divider.py
  python tests/test_terminal_concurrency.py
  python tests/test_resource_bounds.py
  python tests/test_terminal_watchdog.py
  python tests/test_keyboard_leader.py
  ```

- Linux や macOS で動かしてみた方は、結果（PTY / クリップボードの癖など）を
  issue でぜひ教えてください。上の表を ✅ にしていけます。

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
