# Claude Code のセッションが増えすぎた人へ — saikai でターミナルから全部管理する

## はじめに：あなたも「セッション迷子」になっていませんか

Claude Code を使い始めてしばらくすると、こんな状況に陥ります。

- `~/.claude/projects` の下に JSONL ファイルが何十個も増殖している
- 「あの認証トークンのバグを直したやつ、どのセッションだっけ？」がわからない
- `claude --resume` は便利なのに、セッション ID を覚えていないと意味がない
- 別のプロジェクトのセッションに切り替えたいが、また最初から説明し直すのが面倒

私も同じ状況に陥り、**saikai** を作りました。

## saikai とは

https://github.com/m-morino/saikai

saikai（再開・再会）は、Claude Code のセッション履歴をターミナルで一覧・検索・再開するための TUI ツールです。

![saikai のセッションブラウザ](https://raw.githubusercontent.com/m-morino/saikai/master/docs/assets/saikai-browse.svg)

主な特徴：

- **全セッションを 1 つの表に** — `~/.claude/projects` 以下の全 JSONL を読み、最終アクティビティ順にソート
- **リアルタイム絞り込み** — 文字を打つだけでタイトル・内容でフィルタリング
- **日付 / プロジェクト / トピックでグループ化** — `Shift+F7` でグルーピングを切り替え
- **`Enter` 一発で再開** — `claude --resume` を、そのセッションが始まった git ワーキングツリーから起動
- **split-live モード（デフォルト）** — リストの隣にライブ `claude` ペインをタブで並べ、コンテキストを失わず行き来できる
- **キーボードファースト** — マウスなしで全操作可能（後述）

## インストール

Python 3.11 以上が必要です。最短コマンド：

```bash
# uv（推奨）— クローン不要で PATH に `saikai` コマンドが追加される
uv tool install git+https://github.com/m-morino/saikai
```

pip / pipx でも OK：

```bash
pipx install git+https://github.com/m-morino/saikai
```

## 基本的な使い方

```bash
saikai              # 現在の git リポジトリに関連するセッションだけ表示
saikai --all-projects   # ~/.claude/projects 以下の全セッションを表示
```

### キー操作（基本）

| キー | 動作 |
|---|---|
| `↑` `↓` | セッション移動 |
| `Enter` | 選択セッションを再開 |
| `/` または任意の文字 | 検索バーを開く |
| `Tab` | プレビュー（全文 ↔ サマリ切り替え） |
| `?` | ヘルプ（全キーマップをライブ表示） |
| `Esc` | 終了 |

### 検索トークン

テキスト絞り込みと組み合わせて使えます：

```
auth :fav        # 「auth」を含むお気に入りセッション
:open :recent    # 開いていて最近アクティブなセッション
```

## split-live モード

デフォルトで有効になっています。`Enter` でセッションを選ぶと、右ペインにライブな `claude` プロセスが起動します。

![split-live モード](https://raw.githubusercontent.com/m-morino/saikai/master/docs/assets/saikai-split-live.svg)

| マーカー | 意味 |
|---|---|
| `~` | 作業中（ブレインスピナーが回っている） |
| `?` | 入力待ち |
| `!` | 完了・未読 |
| `@` | 開いている |

複数セッションをタブで管理でき、`F2`/`F3` でタブ切り替え、`Shift+F4` で前回のペインセットを丸ごと復元できます。

PTY 依存（`pyte` + `pywinpty`/`ptyprocess`）が不要な場合は：

```bash
SAIKAI_SPLIT_LIVE=0 saikai  # リストのみモード（Enter で全画面再開）
```

## キーボードファースト設計

saikai はマウスなしで全操作できます。**Space がリーダーキー**になっていて、続けて 1 文字を押すとアクションを実行します：

| | | | |
|---|---|---|---|
| `Space f` ★お気に入り | `Space h` 非表示 | `Space e` 名前変更 | `Space r` 再読み込み |
| `Space s` ソート列切り替え | `Space o` ソート順反転 | `Space g` グループ切り替え | `Space t` ツリー表示 |
| `Space n` 新規セッション | `Space p` ペイン復元 | `Alt+←` / `Alt+→` | リスト幅調整 |

最初の数回は使い方ヒントが自動表示されるので、覚えなくても始められます。`?` でいつでも全マップを確認できます。

マウスがあれば列ヘッダーでソート、区切り線をドラッグしてリスト幅を変更できます。マウスは「ボーナス」であって必須ではない、という設計思想です。

## ccmanager との使い分け

[ccmanager](https://github.com/kbwo/ccmanager)（1,100+ ★）はライブのマルチエージェント管理ツールです。saikai との関係は**補完**です：

| | saikai | ccmanager |
|---|---|---|
| 主な目的 | **過去の履歴を探して再開** | **アクティブなエージェントを束ねる** |
| 対象 | `~/.claude/projects` の全 JSONL | 現在起動中のセッション群 |
| PTY | split-live で使用 | コア機能 |

「どの会話だったっけ？」→ saikai  
「複数エージェントを並列で動かしたい」→ ccmanager

両方入れておくと、Claude Code 生活がかなり快適になります。

## 技術メモ

興味のある方向けに：

- **純 Python + [Textual](https://github.com/Textualize/textual)** — Electron 不要、ターミナルだけで動く
- **ファイル読み取り専用** — claude 本体の JSONL をそのまま読むだけ。データベースなし、デーモンなし
- **Windows は ConPTY、Linux/macOS は POSIX PTY** — WezTerm で日常利用中
- **PEP 723 インラインヘッダー** — `uv run saikai.py` で依存を自動解決
- **MIT ライセンス**（pyte は LGPL-3.0 を独立パッケージとして利用）

## おわりに

saikai は「マシン上の全 Claude Code 会話を 1 コマンドで掘り起こせる」ことを目標に作りました。

Windows で Claude Code を毎日使っている中で生まれたツールなので、まずは Windows が一番安定しています。Linux / macOS でも動作するように作りましたが、PTY やクリップボード周りの動作確認ができていないため、試した方はぜひ [Issue](https://github.com/m-morino/saikai/issues) で報告していただけると助かります。

```bash
uv tool install git+https://github.com/m-morino/saikai
saikai --all-projects
```

---

*English README: https://github.com/m-morino/saikai*
