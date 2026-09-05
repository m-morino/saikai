# Agent Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 現在のエージェントが書いたMarkdownを人が確認し、別エージェントへ作業の要点を渡せるようにする。

**Architecture:** saikaiはメモの準備・確認・確定・投入指示を担当する。要約は現在の会話を持つ元エージェントが行い、会話履歴の変換やTUIへの自動入力を必須にしない。

**Tech Stack:** Python >= 3.11、標準pathlib / json / hashlib / datetime / subprocess、既存Textual、段階BのLaunchRequest。

**Spec:** [設計](../specs/2026-09-05-project-agent-switcher-design.md) の段階C。

## Global Constraints

設計の共通制約をすべて適用。現在の `_B2_HANDOFF_PROMPT` / Checkpointは挙動を変えない。
初版は任意APIへの自動要約呼出し、元CLIへの自動入力、`/clear`、旧会話の削除をしない。
引き継ぎの確定・消費は各々ユーザーが画面で選ぶ操作。実行状況不明の他プロセスを自動停止しない。
同じcwdを共有していても、別エージェントによる並行編集が止まったと自動判定しない。

## ファイル構成

- 新規 `saikai_handoff.py`: メタデータ、draft、確定版、Git照合、指示文。
- 新規 `saikai_handoff_ui.py`: 準備、プレビュー、確定、投入方法の画面。
- 変更 `saikai_launcher.py`, `saikai_launcher_ui.py`: 引き継ぎ操作と次CLIの起動。
- 新規 `tests/test_agent_handoff.py`, `tests/test_agent_handoff_ui.py`。
- 変更 pyproject、CI、pre-push、README英日、ARCHITECTURE、CONTRIBUTING。

## Task C1: プロジェクト内に引き継ぎdraftとメタデータを準備

**Files:** `saikai_handoff.py`, `tests/test_agent_handoff.py`、同梱設定。

**Interfaces:**

```python
from dataclasses import dataclass
from pathlib import Path
from saikai_workspace import Project, ProviderId

@dataclass(frozen=True)
class GitSnapshot:
    head: str | None
    branch: str | None
    dirty: bool | None
    error: str | None

@dataclass(frozen=True)
class HandoffDraft:
    id: str
    project: Project
    source_provider: ProviderId
    source_session_id: str | None
    created_at: str
    git: GitSnapshot
    directory: Path
    draft_path: Path

REQUIRED_SECTIONS = ("目的", "決定事項", "変更したファイル", "検証結果", "残作業")
MAX_HANDOFF_BYTES = 262144
```

`capture_git(cwd: Path) -> GitSnapshot` はGit HEAD / symbolic-ref / statusを各3秒で取得する。
dirty計算は `git status --porcelain --untracked-files=normal -- . :(exclude).saikai/handoffs`
の引数配列で、今回のメモ作成自体をdirty変化に数えない。Git未導入・非repoはnull、エラーはerrorに残す。
`prepare_handoff(project: Project, source_provider: ProviderId, source_session_id: str | None) -> HandoffDraft`
はUUIDサブディレクトリを `mkdir(exist_ok=False)` で新規作成し、既存ファイルを上書きしない。
`render_source_instruction(draft: HandoffDraft) -> str` は元のエージェントへ渡す指示を返す。
`load_draft(project: Project, handoff_id: str) -> HandoffDraft` はUUIDを検証してそのサブディレクトリの
manifestを読み、host/cwd/schema/state=draftを検証する。記入のためにCLIを起動・終了した後も、
この関数で同じdraftを再表示できる。manifestは最大64 KiB、超過や破損を空データに変換しない。

- [ ] **失敗テスト。** TemporaryDirectory内のProjectから準備し、見出しと安全な配置をassertする。

```python
draft = prepare_handoff(self.project, "claude", "session-a")
self.assertTrue(draft.draft_path.is_relative_to(Path(self.project.cwd)))
body = draft.draft_path.read_text(encoding="utf-8")
for section in REQUIRED_SECTIONS:
    self.assertIn("## " + section, body)
self.assertIn("検証していない事項", render_source_instruction(draft))
```

事前に `.saikai` または `handoffs` をsymlinkへしたケースで、外部ファイルを作らず拒否する。
Windowsでsymlink権限がなければ適切にスキップし、パス脱出の純粋検証自体は全OSで走らせる。

- [ ] **赤を確認。** `uv run python tests/test_agent_handoff.py`。
- [ ] **実装。** draft.mdの各見出し下は「未記入」にする。manifest.jsonには以下を保存する。

```json
{
  "schema_version": 1,
  "id": "generated-uuid",
  "host_id": "store-host-id",
  "cwd": "/absolute/project",
  "source_provider": "claude",
  "source_session_id": "session-a",
  "created_at": "2026-09-05T00:00:00Z",
  "git": {"head": null, "branch": null, "dirty": null, "error": null},
  "state": "draft"
}
```

ID/host/cwd/timeは実値を使う。preparedディレクトリの全親を確認してsymlinkを拒否し、
resolved pathがcwd配下にあることを検証する。通常のエージェント編集による取り違えを防ぐための検証であり、
同一ユーザーの悪意あるプロセスに対する完全な隔離境界だとは説明しない。
source instruction本文は次を基本にする。

```text
現在の会話をもとに、指定したdraft.mdへ引き継ぎを書いてください。
目的、決定事項と理由、変更したファイル、実際に行った検証と結果、残作業を記載してください。
検証していない事項は未検証と記してください。推測と確認済みの事実を分けてください。
この依頼のためにコード変更、追加検証、コミット、会話のクリアを行わないでください。
認証情報は記載しないでください。見出しを維持し、本文の「未記入」を置き換えてください。
```

先頭にcwdと安全に引用したdraftの相対パスを付ける。shellコマンドとして組み立てない。
- [ ] **追加試験。** 非Git repo、detached HEAD、Git timeout、読取専用ディレクトリ、同名UUID衝突を検証する。
  git呼出し失敗でメモ作成まで不必要に失敗せず、「Git情報未取得」と分かることをassertする。
- [ ] **緑と同梱。** agent_handoffテストを実行し、モジュールをwheel/sdistへ追加する。
- [ ] **レビュー・コミット。** `feat(handoff): prepare portable project handoff drafts`。

## Task C2: 表示した本文を確認して不変の引き継ぎへ確定

**Files:** `saikai_handoff.py`, `saikai_handoff_ui.py`, `saikai_workspace.py`,
両agent_handoffテスト、`tests/test_worksets.py`。

**Interfaces:**

```python
@dataclass(frozen=True)
class HandoffReview:
    draft: HandoffDraft
    body: str
    sha256: str

@dataclass(frozen=True)
class HandoffRecord:
    id: str
    project: Project
    handoff_path: Path
    sha256: str
    source_provider: ProviderId
    source_session_id: str | None
    created_at: str
    git: GitSnapshot
```

`load_review(draft: HandoffDraft) -> HandoffReview` がUTF-8を上限+1 byteだけ読み、
本文の見出しと空/未記入を検証し、確認対象の本文とdigestを返す。
`finalize_handoff(review: HandoffReview) -> HandoffRecord` は確認時のreview.bodyを保存する。
ファイルの現在本文を読み直して、表示と異なるものを確定しない。
handoff.mdは排他的作成で書き、manifestのstateをreadyへ更新する。readyになるまで消費側に表示しない。
`HandoffReviewScreen(review: HandoffReview) -> bool` は確認/キャンセルを返す。
`load_handoff(project: Project, handoff_id: str) -> HandoffRecord` はmanifestのstate=ready、
UUID/host/cwd/schema、本文digestを検証してレコードを返す。
UIが記憶する最後のhandoff IDはプロジェクトごとのランチャー状態に保存し、本文や認証情報を複製しない。
保存型Projectに `last_handoff_id: str | None = None` を追加し、Aのschema_version=1に対する
後方互換の省略可能フィールドとして扱う。旧ファイルではNoneになる回帰テストを追加する。

- [ ] **失敗テスト。** 表示後にdraftを書き換え、確定版は表示した本文であることをassertする。

```python
review = load_review(draft)
draft.draft_path.write_text("画面に表示していない別内容", encoding="utf-8")
record = finalize_handoff(review)
self.assertEqual(record.handoff_path.read_text(encoding="utf-8"), review.body)
self.assertEqual(record.sha256, review.sha256)
```

空ファイル、256 KiB超、UTF-8不正、見出し不足、未記入のまま、symlink、二重確定、
RichタグやANSI制御文字を含む文面を追加する。確認キャンセル時にはhandoff.mdがないことをassertする。

- [ ] **赤を確認。** `uv run python tests/test_agent_handoff.py`、`uv run python tests/test_agent_handoff_ui.py`。
- [ ] **実装。** bodyをUTF-8へ再エンコードしたSHA-256で固定する。独自の改行変換を後から挟まない。
  指定上限を超えた場合は切り捨てて承認せず、短くするよう案内する。
  handoff.mdをflush/fsyncしてからmanifestを原子的置換する。
  manifest確定前に落ちた場合は未確定として表示し、次回読み込みでreadyに自動昇格しない。
  同じdraftの再確定は拒否し、新規引き継ぎを作る。
  プレビューはRichのText等で文字列として表示し、ANSIを実行しない。制御文字を可視化したことを画面で示す。
- [ ] **UI統合。** LauncherAppに「引き継ぎを準備」「下書きを確認」を追加する。
  provider間送信は行わず、ソース指示を表示・コピーする。コピー失敗時も全文選択できる。
  プロジェクト配下にファイルが作られることと保存先を表示する。
- [ ] **緑とレビュー。** テストを再実行、pyprojectへUIモジュールを同梱し、
  `feat(handoff): review and freeze the exact handoff text` としてコミットする。

## Task C3: 次のエージェントへの投入と変更検出

**Files:** `saikai_handoff.py`, `saikai_handoff_ui.py`, `saikai_launcher.py`,
`saikai_launcher_ui.py`, `saikai_workspace.py`, 両agent_handoffテスト、launcher/worksetsテスト、README等。

**Interfaces:**

```python
@dataclass(frozen=True)
class HandoffCheck:
    allowed: bool
    needs_confirmation: bool
    reasons: tuple[str, ...]
```

`check_handoff(record: HandoffRecord, project: Project, current_git: GitSnapshot) -> HandoffCheck`
はhost/cwd・確定ファイルdigest・Git状態を確認する。
`render_target_instruction(record: HandoffRecord) -> str` は確認済みのhandoff.mdの相対パスを含む指示。
`make_handoff_launch(record: HandoffRecord, project: Project, provider: ProviderId,
target: ResumeTarget, current_git: GitSnapshot, confirmed_change: bool) -> LaunchRequest`
を `saikai_launcher.py` に置く。check不許可・変化未確認ならValueErrorにする。
initial_prompt非対応providerはNoneを返し、UIは同じ指示を画面とクリップボードへ出す。

- [ ] **失敗テスト。** 別host/cwd、本文改変はallowed=False、HEAD/branch/dirty変化と
  Git情報取得失敗はneeds_confirmation=Trueとなることをassertする。
  変化がなくても「現在のファイルと照合」という指示が含まれることを確認する。

```python
check = check_handoff(record, other_project, record.git)
self.assertFalse(check.allowed)
request = make_handoff_launch(record, record.project, "agy", ResumeTarget("new"),
                              record.git, confirmed_change=True)
self.assertIsNone(request.initial_prompt)
self.assertIn("現在のファイル", render_target_instruction(record))
```

- [ ] **赤を確認。** agent_handoff両スイートとlauncherスイート。
- [ ] **実装。** 既定は新規会話。登録会話へ投入する場合もtargetを画面で明示する。
  既定プロンプトは以下を使用する。

```text
指定したhandoff.mdを読み、目的・決定事項・残作業を確認してください。
これは以前の作業の記録です。現在のファイルとGit差分を照合し、食い違いを確認してから続けてください。
記載されていない検証を実施済みとみなさず、現在のプロジェクトの指示と権限設定に従ってください。
```

  accepted Check後も、実際の起動直前にhost/cwd/digestを再確認する。
  ファイルへの誘導は通常のプロンプトとして行い、SYSTEM相当の指示へ昇格させない。
  pickerへの自動投入は行わない。非対応は「起動後にこの指示を貼り付け」と表示する。
  元のエージェントを強制終了せず、同じcwdでの編集を停止したことをユーザーが確認してから起動する。
  利用履歴はmanifestへ別のUTC時刻として残すが、handoff本文とsource情報は変更しない。
- [ ] **緑を確認。** 全新規スイートと既存test_keyboard_leader/test_providers/test_restore_auditを実行する。
- [ ] **実利用試験。** 専用repoでClaude→Codex、Codex→agy、agy→Claudeの各経路を試す。
  元エージェントにメモを書かせる手動手順、コピーだけのフォールバックも試す。
  会話全文や暗黙知が完全に移るとは判定せず、目的・決定理由・未検証事項が伝わるかを確認する。
- [ ] **出荷準備。** py_compile/full suite/wheel importを実行する。READMEにGit除外手順、
  保存先、手動操作、対応表、初版の限界を記載し、ARCHITECTURE/CONTRIBUTING/CI/pre-pushを更新する。
- [ ] **レビュー・コミット。** `feat(handoff): launch another agent with a reviewed project brief`。

## Cの受け入れ条件

履歴DBの解析も別LLMのAPI呼出しもせずに、3方向の引き継ぎができる。
CLI仕様が変わって自動投入できなくても、確定メモとコピー用指示が残って手動で続けられる。
元会話は保持され、確認していない本文を次のエージェントへ渡さない。
