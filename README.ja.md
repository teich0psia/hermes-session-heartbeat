# Hermes Session Heartbeat

[English](README.md) | 日本語

Hermes のメッセージング会話で、エージェントが現在の会話の Session Heartbeat を設定・確認・一時停止・再開・解除するためのプラグインです。たとえば「この会話で CI の完了を定期確認し、完了したら監視を解除して」と依頼できます。

Hermes 標準の [`/heartbeat`](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat/) を、モデルから呼び出せる `session_heartbeat` ツールとして利用します。独自のスケジューラーや cron ジョブは作らず、Hermes 本体のコードも置き換えません。人が手動で操作するだけなら、標準の `/heartbeat` で足ります。

## 対応環境

バージョンは **0.1.0**。検証した Hermes は `v0.21.5+4775.g3ebbaf5`（コミット `3ebbaf524344f93943169e63854cb952541563f9`）です。Heartbeat の内部 API を利用するため、ほかの Hermes バージョンでの互換性は未確認です。

| 利用場所・構成 | 対応 |
| --- | --- |
| 常駐するメッセージング Gateway の現在の会話 | 会話 DB とルーティング情報が同じプロファイルホームにあり、標準の `$HERMES_HOME/sessions` を使う場合に対応 |
| 複数プロファイルを扱う Gateway（multiplex） | 上記の同一ホーム条件を満たす場合のみ対応。別ホームのルーティング情報は探索しない |
| CLI / TUI / Desktop / API / ACP | 非対応。標準の `/heartbeat` 自体の対応範囲とは異なる |
| cron / one-shot / サブエージェント | 非対応 |
| 独自のセッション保存先・別ホームをまたぐ会話 | 非対応 |

隔離テストでは Telegram 形式の会話を使っています。各メッセージングサービスでの実送信を確認したものではありません。新規セッションでのモデル向けツール表示、実モデル実行、実際の定期起動・メッセージ配信は未検証です。検証範囲は [検証と互換性](docs/verification.md) に記載しています。

## インストール

[Hermes のプラグイン管理](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)を使います。プラグイン本体はリポジトリ直下ではなく **`plugin/`** にあります。追加の Python パッケージや、このプラグイン専用の API キーは不要です。実行時には Hermes と、対象 Gateway の通常のモデル・メッセージング設定が必要です。

```sh
# インストールのみ。有効化はまだ行わない
hermes plugins install 'https://github.com/teich0psia/hermes-session-heartbeat.git#plugin' --no-enable

# 内容を確認してから有効化。組み込みツールの上書き権限は与えない
hermes plugins enable session-heartbeat --no-allow-tool-override

# ユーザープラグイン一覧を確認
hermes plugins list --user --json
```

特定の版に固定する場合は、install コマンドに `--ref` と公開コミットの完全な **40 桁 SHA** を指定します。ブランチ名・タグ名・短縮 SHA は指定できません。名前付きプロファイルに導入する場合は、各コマンドに `hermes --profile <プロファイル名>` を使い、導入先と Gateway の利用プロファイルを合わせてください。

有効化は信頼した Python コードの実行を許可する操作です。また、稼働中の Gateway ではプラグインのホットリロードを要求することがあります。検証したホストでは再起動なしで登録できましたが、**Python ツールの利用は新規セッションまで保留**されました。既存会話にツールが追加されたと判断したり、動作確認のために会話を無断でリセットしたりしないでください。導入・有効化だけでは定期作業を開始しません。

このリポジトリは native directory plugin であり、`pip install` 用のパッケージではありません。

## 会話での使い方

対応するメッセージング会話で、エージェントに次のように依頼します。

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
| `status` | 現在の設定を確認する。設定がなければ `heartbeat` は `null` |
| `set` | `interval` と空でない `prompt` の両方が必須。会話ごとに一つの設定を置き換え、タイマーと実行回数をリセットする |
| `pause` | 指示を保持して、次回以降の定期実行を停止する |
| `resume` | タイマーを現在時刻から再計算して再開する。停止中の古い予定を即時実行しない |
| `clear` | 設定を解除する。未設定でも成功し、変更は行わない |

間隔は `90s`、`10m`、`2h`、`1d` などで指定し、最小は 60 秒です。間隔だけ、または指示だけを変更したい場合も、`set` に両方を指定します。未設定での `pause` / `resume` は `no_heartbeat` エラーになります。

## 定期実行の注意点

- ユーザーが許可した定期作業にだけ使います。指示には終了条件を含め、不要になったら `clear` で解除してください。
- 定期実行は通常のモデル呼び出し・ツール実行を伴うため、利用量や費用が発生し得ます。プロンプトに秘密値を含めないでください。
- Gateway が動作している間、Hermes 標準の監視処理が保存済み設定を検出します。会話が処理中、またはユーザー入力が待機中なら実行を延期します。指定間隔ちょうどの実行を保証するものではありません。
- `pause` / `clear` は、すでに開始された処理をキャンセルしません。手動操作には標準の `/heartbeat status`、`/heartbeat pause`、`/heartbeat clear` も利用できます。
- セッション ID・プロファイル・パスは引数に指定できません。ホストが渡す現在の会話を検証し、別会話やサブエージェントからの操作を拒否します。
- 保存前と保存後に会話の所有関係を確認しますが、標準コマンドとの並行操作すべてを原子的に保護するものではありません。

## 結果とエラー

成功結果には `ok`、`action`、`changed`、`session_id`、`heartbeat`、`persisted`、`next_due_in_seconds`、`driver`、`wakeup` が含まれます。`persisted: true` は DB の読み戻しで保存状態を確認したという意味であり、モデル応答やメッセージ配信の成功を示しません。

失敗結果は `ok: false`、`error_code`、`error` を返します。

| エラーコード | 確認すること |
| --- | --- |
| `invalid_arguments` | 操作名、間隔、必須の指示を確認する。`set` 以外には間隔・指示を渡さない |
| `unsupported_surface` | 対応する常駐メッセージング Gateway の会話か確認する |
| `missing_session_context` / `profile_mismatch` | ホストが渡す会話情報とプロファイルが一致していない。ID を手入力して回避しない |
| `not_current_route` | 現在の会話に有効なルートがあるか、標準の同一ホーム構成か確認する |
| `no_heartbeat` | `pause` / `resume` の前に設定が必要 |
| `persistence_unverified` / `route_changed` | 保存や会話の状態が競合した可能性がある。`status` で確認し、変更操作を無条件で繰り返さない |
| `invalid_stored_state` / `storage_error` | ホストの保存領域を調査する。破損データを自動上書きしない |
| `unsupported_host` | 必要な Hermes 内部 API が利用できない。検証済みホストとの差を確認する |

## 開発・検証

ソース・テスト・隔離検証スクリプトを同梱しています。環境要件と実行方法、模擬処理と実ホスト API の区別は [検証と互換性](docs/verification.md) を参照してください。

## ライセンス

[MIT License](LICENSE)。
