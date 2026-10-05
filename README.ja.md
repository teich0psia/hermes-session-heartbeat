# Hermes Session Heartbeat

[English](README.md) | 日本語

Hermes のメッセージング Gateway、native Desktop、対話型の classic CLI で、エージェントが現在の会話の Session Heartbeat を設定・確認・一時停止・再開・解除するためのプラグインです。たとえば「この会話で CI の完了を定期確認し、完了したら監視を解除して」と依頼できます。

Hermes 標準の [`/heartbeat`](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat/) を、モデルから呼び出せる `session_heartbeat` ツールとして利用します。独自のスケジューラーや cron ジョブは作らず、Hermes 本体のコードも置き換えません。人が手動で操作するだけなら、標準の `/heartbeat` で足ります。

## 対応環境

バージョンは **0.2.0**。隔離検証の範囲で独立再レビューと保守担当者の受入を完了しています。これは実 UI、モデル実行、配信、稼働中ホストでの採用を確認したという意味ではありません。検証した Hermes は `v0.21.5+4775.g3ebbaf5`（コミット `3ebbaf524344f93943169e63854cb952541563f9`）です。ホスト（Hermes）の非公開 API を利用するため、ほかの Hermes バージョンでの互換性は未確認です。

| 利用場所・構成 | 対応 |
| --- | --- |
| 常駐するメッセージング Gateway の現在の会話 | 会話 DB とルーティング情報が同じプロファイルホームにあり、標準の `$HERMES_HOME/sessions` を使う場合に対応 |
| 複数プロファイルを扱う Gateway（multiplex） | 上記の同一ホーム条件を満たす場合のみ対応。別ホームのルーティング情報は探索しない |
| Native Desktop の現在の会話 | 実行中の UI レコード、現在の agent、同一プロファイルの DB と native lease が一致する場合に対応。標準の通知 poller が保存状態を検出する |
| 対話型の classic CLI | 呼び出し時の native REPL binding を検証し、キャッシュされた manager を更新する。`set` / `resume` は標準 watchdog を起動する |
| `--resume` で開き直した CLI | 保存状態は残るが、watchdog の起動には明示的な `resume` または `set` が必要。`status` では起動しない |
| API / ACP / その他のローカル画面 | 非対応、または対応を保証しない |
| cron / one-shot / サブエージェント | 非対応 |
| 独自のセッション保存先・別ホームをまたぐ会話 | 非対応 |

隔離テストでは通常のツール dispatch と native driver の待機中の実行受付を確認しています。描画された Desktop、実対話端末、会話全体の実行、圧縮用モデル、モデル向けツール表示、実推論・外部配信は未検証です。検証範囲は [検証と互換性（英語）](docs/verification.md) に記載しています。

同一 DB の現在の所有者から続く native 圧縮先（live tip）を検証し、UI / CLI の再接続前も含めて操作を許可します。プラグイン自身は所有者、CLI キャッシュ、ルーティングインデックスの付け替えを行いません。Native CLI settlement は owner ID を更新しますが、lease は元の ID のまま残します。アダプターは、同一ホームの正確なレジストリ lease（PID/live identity）、native の live 圧縮先、およびその継続先に対する競合 lease の不在を検証します。分岐、解放済みまたは欠落した lease、別ホームの所有者は拒否します。レジストリ読み取り時に lease の破棄や譲渡は行わず、競合するエントリが不確実な場合でも保守的に拒否します。所有関係の欠如や陳腐化は推測せず拒否します。

## インストール

[Hermes のプラグイン管理](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)を使います。プラグイン本体はリポジトリ直下ではなく **`plugin/`** にあります。追加の Python パッケージや、このプラグイン専用の API キーは不要です。実行時には Hermes の通常のモデル設定と、利用する画面・通信経路の設定が必要です。次のコマンドは GitHub の既定ブランチを導入するため、manifest の版を確認するか、公開コミットに固定してください。

```sh
# インストールのみ。有効化はまだ行わない
hermes plugins install 'https://github.com/teich0psia/hermes-session-heartbeat.git#plugin' --no-enable

# 内容を確認してから有効化。組み込みツールの上書き権限は与えない
hermes plugins enable session-heartbeat --no-allow-tool-override

# ユーザープラグイン一覧を確認
hermes plugins list --user --json
```

特定の版に固定する場合は、install コマンドに `--ref` と公開コミットの完全な **40 桁 SHA** を指定します。ブランチ名・タグ名・短縮 SHA は指定できません。名前付きプロファイルに導入する場合は、各コマンドに `hermes --profile <プロファイル名>` を使い、導入先と会話の利用プロファイルを合わせてください。

有効化は信頼した Python コードの実行を許可する操作で、Gateway / serve backend の supported hot reload を要求することがあります。プラグイン自体が常にサービス再起動を必要とするわけではありませんが、各プロセスでの採用は reload 応答などで確認してください。**Python ツールの利用は新規セッションまで保留**されます。CLI 更新後は新しいプロセス、Desktop では backend の採用と新しい会話が必要です。activation が失敗した場合の再起動は別途承認を受け、確認だけのために既存会話をリセットしないでください。導入・有効化だけでは定期作業を開始しません。

このリポジトリは native directory plugin であり、`pip install` 用のパッケージではありません。

## 会話での使い方

対応する会話で、エージェントに次のように依頼します。

> この会話で、10 分ごとに CI の完了を確認して。意味のある変化だけ報告し、完了したら heartbeat を解除して。

エージェントが呼び出すツール名・ツールセット名は、どちらも `session_heartbeat` です。このプラグイン独自のスラッシュコマンドは追加しません。次はツール呼び出しの引数例であり、シェルで実行するコマンドではありません。

```json
{"action":"status"}
```

```json
{"action":"set","interval":"10m","prompt":"CI の完了を確認し、意味のある変化だけ報告する。完了したら session_heartbeat の clear を呼ぶ。"}
```

```json
{"action":"pause"}
```

```json
{"action":"resume"}
```

```json
{"action":"clear"}
```

| 操作 | 動作 |
| --- | --- |
| `status` | 現在の設定を確認する。設定がなければ `heartbeat` は `null`。CLI watchdog は起動しない |
| `set` | `interval` と空でない `prompt` の両方が必須。会話ごとに一つの設定を置き換え、タイマーと実行回数をリセットする |
| `pause` | 指示を保持して、次回以降の定期実行を停止する |
| `resume` | タイマーを現在時刻から再計算して再開する。停止中の古い予定を即時実行せず、対話 CLI の watchdog を起動する |
| `clear` | 設定を解除する。未設定でも成功し、変更は行わない |

間隔は `90s`、`10m`、`2h`、`1d` などで指定し、最小は 60 秒です。間隔だけ、または指示だけを変更したい場合も、`set` に両方を指定します。未設定での `pause` / `resume` は `no_heartbeat` エラーになります。

## 定期実行の注意点

- ユーザーが許可した定期作業にだけ使います。指示には終了条件を含め、不要になったら `clear` で解除してください。
- 定期実行は通常のモデル呼び出し・ツール実行を伴うため、利用量や費用が発生し得ます。プロンプトに秘密値を含めないでください。
- 会話を所有するプロセスが動作している必要があります。Gateway / Desktop の標準 poller、または CLI の標準 watchdog が定期実行を扱います。会話が処理中、またはユーザー入力が待機中なら実行を延期します。指定間隔ちょうどの実行を保証するものではありません。
- `pause` / `clear` は、すでに開始された処理をキャンセルしません。手動操作には標準の `/heartbeat status`、`/heartbeat pause`、`/heartbeat clear` も利用できます。
- セッション ID・プロファイル・パスは引数に指定できません。ホストが渡す現在の会話を検証し、別会話やサブエージェントからの操作を拒否します。
- 保存前と保存後に会話の所有関係を確認し、正確に永続化された状態を検証しますが、標準コマンド、リセット、スケジューラーの受付および消費をまたぐ並行操作を原子的なトランザクションとして保護するものではありません。受付済みの待機中 CLI プロンプトは単なる平文であり、世代境界で保護された envelope ではありません。受付後の並行 switch / reset は本プラグインによって原子的に保護されません。
- Desktop の親 / compute-child のポーリングに相当する二つの idle UI poller が同期して DB を読むハーネス環境では、1 回の予定到来に対して 2 回の実行受付が生じる native 制約を再現しました。完全な compute-host の起動やプロセス間での exactly-once な所有権は未検証です。定期作業が exactly-once で実行されることに依存しないでください。こうしたホストドライバーの保証を修正するには、プラグインによる monkeypatch ではなく、別途承認を受けた Hermes 本体の改修が必要です。

## 結果とエラー

成功結果には `ok`、`action`、`changed`、`session_id`、`heartbeat`、`persisted`、`next_due_in_seconds`、`driver`、`wakeup` が含まれます。`persisted: true` は DB の読み戻しで保存状態を確認したという意味であり、モデル応答やメッセージ配信の成功を示しません。

CLI の `set` / `resume` は標準 watchdog の起動も確認しますが、継続的なスレッド監視ではありません。`Thread.start()` の失敗でホストの native な起動ラッチ（start latch）が残った場合、プラグインは native のラッチを変更したりメソッドを置換したりせず、その**実行中の CLI 所有者**で観測された起動失敗を、圧縮やプラグイン再読込みをまたいで記憶します。以後の `set` / `resume` は状態の保存と読み戻しを行いますが、起動成功ではなく `driver_arm_failed` を返します。所有関係と保存領域が正常であれば `status`、`pause`、`clear` は引き続き利用できます。この保守的な失敗記録はその CLI 所有者が破棄されるまで持続します。再試行するには CLI を開き直し、明示的に `resume` または `set` を実行してください。別の CLI 所有者の watchdog から対象の起動を証明することはできません。同一所有者での自動修復には別途 Hermes 本体の承認が必要です。プラグインはホストの起動ラッチやメソッドを修復・置換しません。

失敗結果は `ok: false`、`error_code`、`error` を返します。

| エラーコード | 確認すること |
| --- | --- |
| `invalid_arguments` | 操作名、間隔、必須の指示を確認する。`set` 以外には間隔・指示を渡さない |
| `unsupported_surface` | 対応する Gateway / Desktop / 対話 CLI の現在の所有者を確認できるか確認する。headless・委譲された実行は非対応 |
| `missing_session_context` / `profile_mismatch` | ホストが渡す会話情報とプロファイルが一致していない。ID を手入力して回避しない |
| `not_current_route` | 現在の会話に有効なルートがあるか、標準の同一ホーム構成か確認する |
| `no_heartbeat` | `pause` / `resume` の前に設定が必要 |
| `persistence_unverified` / `route_changed` | 保存や会話の状態が競合した可能性がある。`status` で確認し、変更操作を無条件で繰り返さない |
| `invalid_stored_state` / `storage_error` | ホストの保存領域を調査する。破損データを自動上書きしない |
| `driver_arm_failed` | CLI の状態は保存・読み戻し済みだが、現在の所有者で watchdog の起動に失敗したか、以前の失敗が記録されている。`persisted: true`、`driver_armed: false` と操作内容・セッション・設定・driver を返し、起動成功は主張しない |
| `unsupported_host` | 必要な Hermes 内部 API が利用できない。検証済みホストとの差を確認する |

## 開発・検証

ソース・テスト・隔離検証スクリプトを同梱しています。環境要件と実行方法、模擬処理と実ホスト API の区別は [検証と互換性（英語）](docs/verification.md) を参照してください。

## ライセンス

[MIT License](LICENSE)。
