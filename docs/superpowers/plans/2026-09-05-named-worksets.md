# Named Worksets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claudeの作業セットを名前で保存し、部分失敗で保存内容を失わずに復元する。

**Architecture:** 永続モデルとファイル保存を新しい `saikai_workspace.py` に分離する。既存PickerAppはデータの収集と表示だけを追加し、起動は既存のゲート付き経路を使う。

**Tech Stack:** Python >= 3.11、dataclasses / json / tempfile / os、fcntlまたはmsvcrt、既存Textual / platformdirs。

**Spec:** [設計](../specs/2026-09-05-project-agent-switcher-design.md) の共通制約・段階A。

## Global Constraints

設計の共通制約をすべて適用。名前付きセットは明示保存時だけ変更する。
追加依存なし。UIスレッドで保存ロックやファイルI/Oを待たない。
既存 `open-panes.json` と名前付きセットの保存経路を分ける。

## ファイル構成

- 新規 `saikai_workspace.py`: モデル、検証、JSON永続化、2秒上限のOSロック。
- 新規 `saikai_workset_ui.py`: 名前入力、セット一覧、復元プレビューのModalScreen。
- 変更 `saikai.py`: PickerAppのセット保存・復元アクション。既存前回復元は維持。
- 変更 `pyproject.toml`: 新規モジュールをwheel/sdistに同梱。
- 変更 `.github/workflows/ci.yml`、`.githooks/pre-push`: 新規モジュールをcompile対象へ追加。
- 新規 `tests/test_worksets.py`、`tests/test_workset_ui.py`: ファイル・競合・Pilot試験。
- 変更 `README.md`、`README.ja.md`、`docs/ARCHITECTURE.md`、`CONTRIBUTING.md`: 保存契約と実行方法。

## Task A1: 保存モデルと破損・競合に強いストア

**Files:** `saikai_workspace.py`, `tests/test_worksets.py`, `pyproject.toml`。

**Interfaces:** この段階で以下を確定し、B/Cでも使用する。

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

ProviderId = Literal["claude", "codex", "agy"]
ResumeMode = Literal["new", "id", "picker", "continue"]

@dataclass(frozen=True)
class ResumeTarget:
    mode: ResumeMode
    session_id: str | None = None

@dataclass(frozen=True)
class Project:
    id: str
    host_id: str
    cwd: str
    title: str
    last_provider: ProviderId | None
    targets: dict[str, ResumeTarget]

@dataclass(frozen=True)
class WorksetEntry:
    id: str
    host_id: str
    cwd: str
    provider: ProviderId
    target: ResumeTarget
    title: str

@dataclass(frozen=True)
class Workset:
    id: str
    name: str
    entries: tuple[WorksetEntry, ...]

@dataclass(frozen=True)
class WorkspaceState:
    schema_version: int
    revision: int
    host_id: str
    projects: tuple[Project, ...]
    worksets: tuple[Workset, ...]
```

`WorkspaceStore(path: Path)` は `initialize() -> WorkspaceState`、`read() -> WorkspaceState`、
`update(expected_revision: int, change: Callable[[WorkspaceState], WorkspaceState]) -> WorkspaceState` を提供する。
`initialize` だけが欠落ファイルをschema_version=1、revision=0、UUID host_idで初期化する。
`read` は書き込まない。`update` がrevisionを1増やす。callbackへ渡すデータはdeep copyし、
呼出し側のdict変更がストアに影響しないようにする。
公開例外は `StoreCorruptError`, `StoreVersionError`, `StoreConflictError`, `StoreBusyError`。
通常のI/O例外は原因を保って呼出し側へ返す。

- [ ] **失敗テストを作る。** `tests/test_worksets.py` はunittestの直接実行形式にする。

```python
def test_conflicting_writer_cannot_replace_saved_set(self):
    store = WorkspaceStore(self.root / "workspaces.json")
    first = store.initialize()
    entry = WorksetEntry("entry-a", first.host_id, str(self.root), "claude",
                         ResumeTarget("id", "session-a"), "A")
    saved = store.update(first.revision, lambda s: replace(
        s, worksets=(Workset("set-a", "個人開発", (entry,)),)))
    with self.assertRaises(StoreConflictError):
        store.update(first.revision, lambda s: replace(s, worksets=()))
    self.assertEqual(store.read(), saved)
```

テストクラスのsetUpはTemporaryDirectoryとself.rootを作成し、dataclasses.replaceと公開型をimportする。
破損JSON、schema_version=2、空白だけの名前、80文字を超える名前、重複したset ID、
不正provider、idモードなのに空ID、非idモードなのにIDありを個別に拒否するテストも追加する。
名前の一致は前後空白除去後の完全一致。重複名は新規保存時に拒否し、更新はUUIDで指定する。

- [ ] **赤を確認。** `uv run python tests/test_worksets.py` → 未実装のimportで失敗。
- [ ] **最小実装。** JSONのトップに上記フィールドを持たせ、常に全スキーマを検証する。
  `path.with_suffix(".lock")` をOSロック対象にし、Windowsは先頭の1バイトをロックする。
  OSロックファイルを削除しない。0.05秒間隔、time.monotonicによる2秒期限で再試行する。
  lock内でread→revision照合→change→検証→一時ファイルへUTF-8 JSON→flush/fsync→os.replaceを行う。
  同一ディレクトリにtempfileを作り、失敗時はその一時ファイルだけを片付ける。
  `saikai.py` の `_write_text_atomic` をimportして循環依存を作らず、同じ原子的置換方式をここで実装する。
- [ ] **競合試験。** multiprocessingで二つのwriterを同一revisionから実行し、片方だけ成功、
  他方はStoreConflictErrorとなることをassertする。異常終了したロック所有プロセスの後で
  次のwriterが取得できること、lockの2秒タイムアウト、replace失敗で旧ファイルが残ることを確認する。
- [ ] **緑と同梱確認。** 上記テストを再実行し、新規モジュールをwheel/sdistリストへ追加する。
- [ ] **レビュー・コミット。** `feat(worksets): persist versioned named worksets`。このタスク以外の差分は含めない。

## Task A2: 開いているClaudeペインを名前付きセットへ保存

**Files:** `saikai.py`, `saikai_workset_ui.py`, `tests/test_workset_ui.py`, `pyproject.toml`。

**Interfaces:** `WorksetNameScreen(existing_names: tuple[str, ...], initial: str = "") -> str | None`、
`WorksetListScreen(worksets: tuple[Workset, ...]) -> str | None`（戻り値はset ID）。
PickerAppに `action_save_workset()`、`action_manage_worksets()` を追加する。
既存の `LiveSessionManager.all_terms()`、term.sid / term._cwd、`_sid_index`を利用する。
term._cwdを保存する（現在のAgentTerminalに公開cwd属性はない）。表示用タイトルからパスを逆算しない。
保存用cwdがないtermは理由付きで除外し、全件除外なら空保存として拒否する。

- [ ] **失敗テスト。** 実PickerAppをPilotで起動し、fake terminal二つを登録する。
  `action_save_workset` から名前「個人開発」を入力し、保存ファイルに両sidと正しいcwdがあることをassertする。
  保存後の `_on_live_exit` で一方を閉じ、store.readのworksetsが同一であることをassertする。
  fixtureは先行 `tests/test_restore_audit.py` と同じくTemporaryDirectoryへHOME等を隔離する。

```python
before = store.read().worksets
app._on_live_exit("session-a")
await pilot.pause()
self.assertEqual(store.read().worksets, before)
```

- [ ] **赤を確認。** `uv run python tests/test_workset_ui.py` → アクション未実装で失敗。
- [ ] **実装。** リストフォーカス時のSpaceメニューに `w: worksets` を追加し、
  セット一覧に「現在のペインを保存」「前回のペインから保存」「名前変更」「更新」「削除」を置く。
  既存のSpace割当とバインディングIDに衝突がないことを実装時に確認する。
  更新先はUUIDで選び、プレビューに旧項目数と新項目数を表示する。
  `_opening_sids` が非空なら保存を保留する。空保存は拒否する。配列順序は現在のタブ順を維持する。
  メモリ上の値をUI側でコピーし、store操作は `run_worker(..., thread=True, exit_on_error=False)` へ渡す。
  worker結果はUIへ返して通知し、例外でアプリを終了させない。
- [ ] **失敗ケースを確認。** 空集合、不正名前、重複名、同時編集、保存先が読取専用、
  起動受付中、前回データが壊れている場合に既存セットのbytesが変わらないことをassertする。
  スナップショット取込は有効なClaude ID/cwdだけをプレビューし、元のOPEN_PANES_FILEを変更しない。
- [ ] **緑を確認。** `test_worksets.py`、`test_workset_ui.py`、`test_keyboard_leader.py`。
  pyprojectへsaikai_workset_ui.pyを追加する。
- [ ] **レビュー・コミット。** `feat(worksets): save and manage Claude working sets`。

## Task A3: 非破壊の復元プレビューと出荷検証

**Files:** `saikai.py`, `saikai_workset_ui.py`, `tests/test_workset_ui.py`、README英日、
`docs/ARCHITECTURE.md`, `CONTRIBUTING.md`, CI, pre-push。

**Interfaces:** `RestoreRow(entry: WorksetEntry, disposition: str, reason: str)` を
`saikai_workset_ui.py` に定義。dispositionは `ready / already_open / unavailable`。
`RestoreWorksetScreen(rows: tuple[RestoreRow, ...]) -> tuple[str, ...] | None` が選択entry IDを返す。
PickerAppの `action_restore_workset(set_id: str)` は候補を既存起動経路へ渡し、セットは更新しない。

- [ ] **失敗テスト。** 3項目中1件は起動受付、1件は存在しないcwd、1件は容量拒否を作る。

```python
before = store.read().worksets
app.action_restore_workset("set-a")
# Pilotで復元確認ボタンを押した後に検証する。
await pilot.click("#restore-confirm")
await pilot.pause()
self.assertEqual(store.read().worksets, before)
self.assertEqual(launched_ids, ["session-a"])
```

fake spawnは実際の `_open_or_attach_live` の受付bool/None契約を保つ。
他ホストの項目・開始中のsid・同じsidの重複が二重起動されないケースを追加する。

- [ ] **赤を確認。** `uv run python tests/test_workset_ui.py`。
- [ ] **実装。** スキャン済みsessionの有無とは別に、保存cwdが使用可能か調べる。
  index外のClaude会話は既存 `_new_session_stub` で復元する。容量・RAM・他ウィンドウ確認は
  `_open_or_attach_live` を通して維持する。候補の検査はworker、最終起動判断はUIで再確認する。
  名前付きセットを `_restore_candidates` に代入して前回復元の意味を変えない。
  共有が必要なら既存復元のループを `_restore_rows(rows: list[dict]) -> None` に抽出し、
  前回復元と名前付き復元から呼ぶ。戻り値と件数表示は監査修正を維持する。
- [ ] **緑を確認。** workset両テストと `test_restore_audit.py` を実行する。
- [ ] **ドキュメントと同梱。** 保存先、明示更新、旧スナップショット取込、他ホスト拒否を英日で説明する。
  compile対象に `saikai_workspace.py saikai_workset_ui.py` を追加する。
  `uv build` 後、別の一時venvへwheelをインストールし、リポジトリ外で両モジュールをimportする。
- [ ] **全検証。** 全tests/test_*.pyを実行し、未解決HTTP失敗を含め出荷条件を確認する。
  terminal/threading変更が発生した場合はconcurrency/resource_bounds/protocol/watchdog/pty_backendを必ず実行する。
- [ ] **レビュー・コミット。** `feat(worksets): restore saved sets without eroding snapshots`。

## Aの受け入れ条件

「保存→saikai終了→再起動→名前から復元」で対象会話とcwdが一致する。
失敗した項目を直して再試行でき、保存済みセットの項目が勝手に減らない。
名前付きセット未使用時の既存動作が変わらない。
