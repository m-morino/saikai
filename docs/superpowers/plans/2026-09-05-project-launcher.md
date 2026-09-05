# Project Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同じプロジェクトフォルダからClaude / Codex / agyを起動・再開し、CLI終了後に一覧へ戻る。

**Architecture:** `saikai --launcher` で独立Appを起動する。AppはLaunchRequestを返して終了し、UIループの外で公式CLIを前景実行する。既存Claude監督AppとそのPTYは操作しない。

**Tech Stack:** Python >= 3.11、標準subprocess / shutil、既存Textual、段階AのWorkspaceStore。

**Spec:** [設計](../specs/2026-09-05-project-agent-switcher-design.md) の段階B。

## Global Constraints

設計の共通制約をすべて適用。Aの公開型とストアを使用する。
新しいprovider履歴スキャン・PTY描画・リモート通信を実装しない。
各起動では現在ホストIDと保存host_idを照合する。全ファイル操作は対象ホスト上で行う。
起動済みCLIに割り込むホットスイッチ、混在セットの一括起動は初版対象外。

## ファイル構成

- 変更 `saikai_provider.py`: ランチャー用の公開CLIアダプター。既存AgentProviderは維持。
- 新規 `saikai_launcher.py`: LaunchRequest、CLI照会、前景実行、Appとの往復。
- 新規 `saikai_launcher_ui.py`: プロジェクト一覧、providerと再開方法の選択。
- 変更 `saikai.py`: `--launcher` の早期ディスパッチ。
- 使用 `saikai_workspace.py`: Project / ResumeTarget / WorkspaceStore。
- 新規 `tests/test_launcher.py`, `tests/test_launcher_ui.py`。
- 変更 `tests/test_providers.py`、pyproject、CI、pre-push、README英日、ARCHITECTURE、CONTRIBUTING。
- 新規 `docs/CLI-COMPATIBILITY.md`: OS・CLI版・対応機能・確認日の表。

## Task B1: 公式CLIコマンドを小さなアダプターで構築する

**Files:** `saikai_provider.py`, `tests/test_providers.py`, `docs/CLI-COMPATIBILITY.md`。

**Interfaces:** Aの `ResumeTarget` と既存 `LaunchSpec` を使う。
`saikai_provider.py` は `saikai_workspace.py` の型をimportしてよいが、workspaceはproviderをimportしない。

```python
from dataclasses import dataclass
from typing import Mapping
from saikai_workspace import ResumeMode, ResumeTarget

@dataclass(frozen=True)
class LauncherCapabilities:
    resume_modes: frozenset[ResumeMode]
    initial_prompt: bool

class LauncherAdapter:
    id: str
    executable_name: str
    capabilities: LauncherCapabilities
    commands: dict[ResumeMode, tuple[str, ...]]

    def build(self, *, executable: str, cwd: str, env: Mapping[str, str],
              target: ResumeTarget, initial_prompt: str | None = None) -> LaunchSpec:
        if target.mode not in self.capabilities.resume_modes:
            raise ValueError("unsupported resume mode")
        sid = target.session_id
        if target.mode == "id":
            if not sid or sid.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in sid):
                raise ValueError("invalid session id")
        elif sid is not None:
            raise ValueError("session id requires id mode")
        args = [executable]
        for token in self.commands[target.mode]:
            args.append(sid if token == "{id}" else token)
        if initial_prompt is not None:
            if not self.capabilities.initial_prompt or target.mode == "picker":
                raise ValueError("initial prompt is unavailable for this operation")
            args.append("作業指示:\n" + initial_prompt)
        return LaunchSpec(args, cwd, dict(env), sid)
```

公開factoryは `get_launcher_adapter(provider_id: str) -> LauncherAdapter`。
具象型は `ClaudeLauncherAdapter`, `CodexLauncherAdapter`, `AgyLauncherAdapter`。
各具象型のcommandsは次の固定対応表の実行ファイル以降をtupleで保持する。
id指定のトークンだけ `"{id}"` とし、文字列全体に対するformatやシェル展開は行わない。

| provider | new | id | picker | continue | initial_prompt |
|---|---|---|---|---|---|
| claude | `claude` | `claude --resume ID` | `claude --resume` | `claude --continue` | 末尾の位置引数。実機確認を必須にする |
| codex | `codex` | `codex resume ID` | `codex resume` | `codex resume --last` | new/id/continueの末尾位置引数。pickerへの自動投入は不可 |
| agy | `agy` | 初版は未対応 | 初版は未対応（起動後に `/resume` を案内） | `agy --continue` | 初版はfalse。表示・コピーへフォールバック |

cwdはsubprocessのcwd引数で渡す。continueは「公式CLIがそのcwdで選ぶ最新」であり、保存会話IDと同一とは表示しない。
capabilities.initial_promptはnew/id/continueで利用可能という意味。pickerでは全providerで拒否する。
既存ClaudeProviderの新規UUID必須契約をこのnewに流用しない。ランチャーのnewはID未取得で正常。

- [ ] **公開仕様を実機照合。** 各実行環境で `claude --version/--help`、`codex --version/--help`、
  `codex resume --help`、`agy --version/--help` を読む。起動そのものはこの手順で実行しない。
  表と実機が食い違う場合はその操作を未対応とし、既知のコマンドへ推測で置き換えない。
  Windowsの実行パスはnative .exe/.comを対象とし、.cmd/.batは初版では拒否してnativeバイナリの指定を案内する。
- [ ] **失敗テスト。** 引数に空白・日本語を含むcwd、通常のID、初期プロンプトでargv配列をassertする。

```python
adapter = get_launcher_adapter("codex")
spec = adapter.build(executable="codex-test", cwd="C:/work/日本語 project",
                     env={"PATH": "bin"}, target=ResumeTarget("id", "thread-a"))
assert spec.argv == ["codex-test", "resume", "thread-a"]
assert spec.cwd == "C:/work/日本語 project"
assert spec.session_id == "thread-a"
```

agyのid、未知provider、空ID、`-`で始まるID、制御文字を含むIDをValueErrorとするケースを追加する。
実行ファイル・cwd・promptはシェル文字列へ連結されないことを特殊文字入りのargv比較で確認する。

- [ ] **赤を確認。** `uv run python tests/test_providers.py`。
- [ ] **実装。** 固定対応表を小さなメソッドで表し、envはコピーする。
  resume IDの検証はUUID形式に固定せず、空・先頭ハイフン・制御文字を拒否する。
  prompt引数がオプションとして解釈される形で始まる場合は、固定の導入文を前置する。
  未対応モードやpromptはコマンドを実行せずエラーにする。ユーザーの権限設定を勝手に追加しない。
- [ ] **緑を確認。** 既存providerのテストと追加ケースすべてを実行する。
- [ ] **レビュー・コミット。** `feat(launcher): add explicit public CLI launch adapters`。

## Task B2: 専用入口と所有する子プロセスだけの前景実行

**Files:** `saikai_launcher.py`, `saikai.py`, `tests/test_launcher.py`、同梱設定。

**Interfaces:**

```python
from dataclasses import dataclass
from pathlib import Path
from saikai_workspace import Project, ProviderId, ResumeTarget

@dataclass(frozen=True)
class LaunchRequest:
    project: Project
    provider: ProviderId
    target: ResumeTarget
    initial_prompt: str | None = None

@dataclass(frozen=True)
class LaunchResult:
    started: bool
    returncode: int | None
    error: str | None

@dataclass(frozen=True)
class CliInfo:
    executable: str | None
    version: str | None
    error: str | None
```

`probe_cli(provider: ProviderId, executable_override: str | None) -> CliInfo` はPATH解決＋
`--version` を最大3秒で確認する。stdout/stderrは各64 KiB以下だけ保持し、上限超過を検出したら
所有するprobeプロセスを終了・waitする。取得のための補助threadも終了時にjoinする。
結果はApp起動中だけキャッシュする。`--help` の文章をライブ状態判定や毎回の起動条件に使わない。

`build_launch_spec(request: LaunchRequest, executable: str, env: dict[str, str], current_host_id: str) -> LaunchSpec`
はhost_id/cwdを検証し、providerのbuildを呼ぶ。
`run_foreground(spec: LaunchSpec) -> LaunchResult` はUI外で実行。
このタスクではprobe/build/runの部品を単体で完成させる。公開の `--launcher` 入口はB3で追加する。

実行ファイルoverrideは既存config.tomlの `[launcher.executables]` に
`claude = "C:/path/claude.exe"` のように保存する。キーはclaude/codex/agyだけ、
値は空でない絶対パス文字列だけ受け付ける。設定がなければPATHを使用する。
`--launcher` と併用できる引数は `--project PATH`、`--help`、`--version` に限定する。

- [ ] **失敗テスト。** unittest.mockでPopenを差し替え、cwd/env/argvと呼出し順を検証する。

```python
with patch("saikai_launcher.subprocess.Popen") as popen:
    popen.return_value.wait.return_value = 7
    result = run_foreground(LaunchSpec(["agent-test"], str(self.root), {}, None))
    self.assertEqual(result, LaunchResult(True, 7, None))
    self.assertEqual(popen.call_args.kwargs["cwd"], str(self.root))
    self.assertFalse(popen.call_args.kwargs.get("shell", False))
```

別ホスト、消えたcwd、実行ファイルなし、probe timeout、大きすぎる出力のケースを追加する。
他ホストまたはcwd不正の場合はPopenが0回であることをassertする。

- [ ] **赤を確認。** `uv run python tests/test_launcher.py`。
- [ ] **前景実行を実装。** stdin/stdout/stderrは継承。親プロセスでos.chdirを使わない。
  childのPopenハンドルを保持し、wait完了前にLauncherAppを再開しない。
  KeyboardInterruptでは同じ端末により子もSIGINT/CTRL_Cを受ける前提で、親はその子のwaitを続ける。
  親だけが先に戻る既存 `_resume_claude` の例外処理はコピーしない。
  waitが一度KeyboardInterruptを投げて次に終了コード130を返すfake childで、この順序を検証する。
  子の異常終了はLaunchResultに出し、ほかのプロセスを列挙・killしない。
  独自プロセスグループ、デーモン化、DETACHED_PROCESSは使わない。
- [ ] **環境契約を実装。** `build_launch_spec` 内で現在envをコピーし、現在の
  `_child_spawn_env` の親セッション・venv除去ルールを共通利用できるよう整理する。
  共通関数 `launcher_child_env(base: dict[str, str]) -> dict[str, str]` は
  `saikai_provider.py` に置き、元関数も必要な共通部分だけ委譲する。
  authと明示CODEX_HOME/CLAUDE_CONFIG_DIRを維持し、親CLIのセッション識別子だけ除去する。
  providerが再注入する変数名は公式仕様・既存テストで確認したものだけ扱う。
- [ ] **ライフサイクル試験。** fake CLIを現在のsys.executableと一時Pythonスクリプトで作成し、
  stdinを継承した起動・終了コードの保持・cwdの維持を確認する。Popen失敗でstarted=Falseになること、
  実行後も親のcwdとenvが変わっていないことをassertする。
- [ ] **緑と同梱。** test_launcher/test_providers/test_configを実行し、モジュールをwheel/sdistへ追加する。
- [ ] **レビュー・コミット。** `feat(launcher): run agent CLIs outside the picker lifecycle`。

## Task B3: プロジェクト・再開先・混在セットを操作するUI

**Files:** `saikai_launcher_ui.py`, `saikai_launcher.py`, `saikai.py`, `tests/test_launcher_ui.py`,
`saikai_workspace.py`、README英日等。

**Interfaces:** `LauncherApp(store: WorkspaceStore, executable_overrides: dict[str, str],
last_result: LaunchResult | None)` はB2のLaunchRequestまたはNoneを返す。
`run_launcher(store_path: Path, executable_overrides: dict[str, str]) -> int` をsaikai_launcher.pyに実装し、
このApp.runの結果をbuild_launch_spec→run_foregroundへ渡して、終了後に新しいAppインスタンスを作る。
Noneが返ればループを終了する。インポート循環を避けるためLauncherAppは関数内でimportする。
ストアのProjectを登録・名前変更・削除し、targetsはproviderごとに独立したResumeTargetを保持する。
Project削除時にWorksetEntryのコピーを連動削除しない。
追加関数 `register_project(state: WorkspaceState, cwd: str, title: str) -> WorkspaceState` は
現在ホストでresolveしたcwdを使い、同じホスト・同じ正規化パスの重複登録を拒否する。
Windowsではos.path.normcase、POSIXでは大文字小文字を保持して比較する。

- [ ] **失敗テスト。** Pilotで一つのProjectを選び、Claude→newを選択して戻り値を確認する。
  次回AppでCodex→idを登録し、Claude側のResumeTargetが変わらないことをassertする。

```python
saved = store.read().projects[0]
self.assertEqual(saved.targets["claude"], ResumeTarget("new"))
self.assertEqual(saved.targets["codex"], ResumeTarget("id", "thread-a"))
```

未導入agyが選択不可で理由を表示するケース、provider選択をキャンセルしたときPopenしないケース、
起動中Appが存在しないケースを追加する。任意の実セッションをfixtureに使わない。

- [ ] **赤を確認。** `uv run python tests/test_launcher_ui.py`。
- [ ] **CLI入口を追加。** argparseに `--launcher` を追加し、通常のClaude履歴探索前に分岐する。
  上記許可リスト以外の既存引数と併用した場合はparser.errorで拒否する。
  `--project PATH` は初期プロジェクト登録に利用できる。暗黙の全履歴走査はしない。
  `textual_pick`、`_resume_claude`、`action_resume_detached` をランチャーから呼ばない。
  起動結果の通知は「CLI終了コードN」であり「タスク完了」ではない。
- [ ] **実装。** 一覧はプロジェクト名・cwd・最後に選んだproviderを表示する。
  起動方法は「新規」「登録した会話」「公式の会話一覧」「最新の会話」。非対応項目は理由付きで無効化する。
  登録IDなしで「登録した会話」を既定にしない。new成功後もIDは自動取得扱いにしない。
  Worksetの一覧から選んだ項目はそのhost/cwd/provider/targetを使ってLaunchRequestへ変換する。
  混在セットには「項目を選んで起動」と明記し、一括起動ボタンを出さない。
  CLIの開始を確認できた後だけlast_provider/targetsを保存し、保存に失敗してもCLI成功を巻き戻さない。
  初版はrun_foregroundから戻ったstarted=Trueの結果で保存する。起動中に設定を書き換えるworkerを増やさない。
  UI操作は既存ルール同様に通常Ctrl+letterを奪わず、表示ボタンとfunction keyを用いる。
- [ ] **実機確認。** 専用の一時プロジェクトで各CLIの起動・終了→一覧復帰、new/対応resumeを試す。
  Linux SSHは対象ホストで試す。ローカルWindowsだけの成功でSSH対応済みとしない。
  CLI非導入の組み合わせは「未検証」として対応表へ記録し、テスト用fakeの成功と分ける。
- [ ] **緑と出荷。** launcherの2スイート、providers、全test_*.pyを実行する。
  wheelをリポジトリ外から `saikai --launcher --help` で確認する。
  CLI-COMPATIBILITY.md、README、ARCHITECTURE、CONTRIBUTING、CIとpre-pushのcompile対象を更新する。
- [ ] **レビュー・コミット。** `feat(launcher): select projects and agent-specific resume targets`。

## Bの受け入れ条件

同じcwdで3製品の導入済みCLIを選んで起動できる。終了すると一覧に戻る。
別端末のsaikaiが保持するClaude/Remote Controlペインは止まらない。
new、保存ID、picker、continueを表示と保存の両方で区別し、未対応機能を成功扱いしない。
