# Claude Code のセッションが増えすぎた人へ。saikai でターミナルから全部管理する

## セッションが増えてきたあなたへ

`~/.claude/projects` を覗いたら、JSONL ファイルが何十個もある。「あの認証トークンのバグを直したやつ、どのセッションだっけ？」がわからない。`claude --resume` は便利でも、リポジトリや worktree が増えるほど目的の会話を探しづらくなる。

Claude Desktop のように会話を見渡せる画面は便利です。一方で、普段いる Linux
ターミナルの中で、デスクトップアプリを開き続けずに同じ問題を解きたかった。
そのへんが限界になって、**saikai** を作りました。

## saikai とは

https://github.com/m-morino/saikai

saikai（再開・再会）は、Claude Code のセッション履歴をターミナルで一覧・検索・再開するための TUI ツールです。

![saikai のセッションブラウザ](https://raw.githubusercontent.com/m-morino/saikai/master/docs/assets/saikai-browse.svg)

一覧は、**色で文脈、記号で状態**を表します。既定では同じプロジェクトの
タイトルが同じ色です。`~` は作業中、`?` は入力待ち、`!` は完了後まだ応答して
いないセッションなので、次に見る場所を一覧から判断できます。

できること：

- `~/.claude/projects` 以下の全 JSONL を最終アクティビティ順に一覧表示
- 文字を打つとその場でタイトル・内容を絞り込む
- 日付 / プロジェクト / 状態でグループ化（`Shift+F7` で切り替え）
- `Enter` で `claude --resume` を、そのセッションの git ワーキングツリーから起動
- デフォルトで split-live モードがオン。リストの隣にライブ `claude` ペインをタブで並べて行き来できる
- マウスなしで全操作できる（後述）

## インストール

Python 3.11 以上が必要です。最短コマンド：

```bash
# uv（推奨）— クローン不要で PATH に `saikai` コマンドが追加される
uv tool install saikai
```

pip / pipx でも OK：

```bash
pipx install saikai
```

## 基本的な使い方

```bash
saikai              # 全プロジェクト・全履歴を表示（これが既定）
saikai --here       # 現在の git リポジトリのセッションだけに絞る
saikai --days 7     # 直近 7 日だけに絞る
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

## 技術メモ

作りとしては：

- 純 Python + [Textual](https://github.com/Textualize/textual)。Electron なし、ターミナルだけで完結
- claude の JSONL を読み取り専用で参照するだけ。データベースもデーモンも持たない
- PTY は Windows が ConPTY、Linux/macOS が POSIX PTY。WezTerm で毎日使ってる
- PEP 723 インラインヘッダーつきなので `uv run saikai.py` で依存が自動で入る
- MIT（pyte だけ LGPL-3.0 だが独立パッケージとして利用なので saikai 自体は MIT のまま）

## おわりに

Windows で Claude Code を毎日使っていてセッション管理に困ったので作りました。なので Windows が一番安定してます。

Linux / macOS は「動くように書いた」レベルで、PTY やクリップボード周りは自分では確認できていません。試した方、[Issue](https://github.com/m-morino/saikai/issues) に結果を書いてもらえると助かります。動いた・動かなかった、どちらでも。

```bash
uv tool install saikai
saikai
```

---

*English README: https://github.com/m-morino/saikai*
