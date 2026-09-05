# Project Agent Switcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 名前付き作業セット、3エージェントの起動・再開、Markdown引き継ぎを、小さく独立して出荷できる順に追加する。

**Architecture:** 永続データ、provider別コマンド、ランチャーUI、引き継ぎを分離する。既存Claude監督画面を残し、専用ランチャーは自分が所有するCLIだけを前景で実行する。

**Tech Stack:** Python >= 3.11、標準ライブラリ、既存Textual / platformdirs。追加のランタイム依存なし。

**Spec:** [設計](../specs/2026-09-05-project-agent-switcher-design.md)

## Global Constraints

設計の「共通制約」全項目を適用する。現時点では計画のみで、以下は未実装。
既存監査差分を破棄しない。実装開始時に作業ツリーとHEADを再確認する。

## 実行順

| 段階 | 計画 | 出荷できる成果 | 完了条件 |
|---|---|---|---|
| 0 | 本書の開始条件 | 先行監査修正の整理 | 修正テストを保持し、未解決HTTP失敗を記録 |
| A | [名前付き作業セット](2026-09-05-named-worksets.md) | Claudeの作業セットを名前で保存・復元 | 部分失敗や終了でセットが削られない |
| B | [プロジェクトランチャー](2026-09-05-project-launcher.md) | プロジェクトからClaude/Codex/agyを選んで起動 | CLI終了後に一覧へ戻り、既存Claudeペインに触れない |
| C | [Markdown引き継ぎ](2026-09-05-agent-handoff.md) | 確認したメモを次のエージェントへ渡す | 元の会話を消さず、内部履歴形式に依存しない |

### 開始条件

- [x] `git status --short` と `git log -1 --oneline` を確認する。計画作成時HEADは `e36f904`。
- [x] 未コミットの監査修正（saikai.py、saikai_terminal.py、README英日、.gitignore、test_restore_audit.py、監査レポート）を別変更として保持・レビューする。
- [x] `uv run python tests/test_restore_audit.py` を実行し、6件の回帰テストが通ることを確認する。
- [x] 監査時のHTTP接続切断2件を別の正常な通信環境で確認する。解決前もfake CLIによる開発は可能だが、リリースの全テスト成功条件は免除しない。
- [x] 実装を始める段階で隔離worktreeを用意する。監査差分を取り込む方法を明示し、HEADだけから作ったworktreeで修正を落とさない。

## 最初の到達点

監査修正は `codex/audit-baseline` の `426353b` に保存した。このコミットから `.worktrees/named-worksets`（`codex/named-worksets`）を作成し、計画文書もfast-forwardで取り込む。元のmasterは変更せず、pushは行わない。

開始条件の確認記録（2026-09-05）: 基準HEADは `e36f904`。監査差分は独立レビュー済みで、回帰テスト6件は成功。Windowsでは全23テストファイル中21件成功し、`test_mirror_hub.py` と `test_mirror_input.py` は失敗した。標準ライブラリでも接続終了時の受信失敗を再現したが、具体的原因は未確定。

指定SSH先の既存クローンから `e36f904` の一時worktreeを作成し、既存venv（Linux／Python 3.11.14）でHTTPテスト2件を実行した。両方とも終了コード0で成功。対象モジュールとテストはローカルでも基準HEADから未変更。コード転送は行っていない。SSH先masterの独自コミット2件は保持した。Windows固有の失敗が解消したことや、Linuxで全スイートが成功したことを意味しない。A/B/Cの実装はまだ開始していない。

最初はAだけを実装・実利用する。B、Cはこの計画に沿って追加するが、Aの完成を待たせない。
工数見積もりより、各段階の受け入れ条件を判断基準にする。
追加API課金、実セッションへの自動プロンプト送信、外部告知は計画作成に含まれない。

## 設計判断の要点

1. 名前付きセットと「前回開いていたペイン」は別保存。復元による上書きを防ぐ。
2. 初版のランチャーは公式CLIを前景で使う。高速なホットスイッチ・混在一括起動は約束しない。
3. Codex/agyのIDが未取得なら公式picker/continueを使い、特定会話を保存したと表示しない。
4. 引き継ぎは人が確認するMarkdown。指示のコピーによる操作を必ず残す。
5. SSH先を操作するときは、そのSSH先でsaikaiを起動する。公式の遠隔接続方式を再実装しない。

## 計画セルフレビュー

- [x] 設計のA/B/Cに実装タスクを対応付けた。
- [x] 作業セットの不変性、混在セットの制約、ID未取得、子プロセスの所有権を具体化した。
- [x] 保存・起動・引き継ぎのインターフェースを各計画で定義した。
- [x] 既存監査差分、未解決テスト、wheel同梱、CIと実機確認を出荷条件に含めた。
