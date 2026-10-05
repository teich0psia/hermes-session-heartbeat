# 検証と互換性

## 確認済みの範囲

検証対象の Hermes は `v0.21.5+4775.g3ebbaf5`、コミット `3ebbaf524344f93943169e63854cb952541563f9` です。既存の開発・独立レビュー・導入時の記録では、隔離テスト **30 件成功**、Plugin Doctor **1 tool / 0 hooks / 終了コード 0** を確認しています。導入したコードと受入済みソースの一致も確認しています。

テストはリポジトリの `tests/test_heartbeat.py` にあります。

- 実 PluginManager による発見・登録と、registry 経由の全操作。
- 実 SessionDB と HeartbeatManager による保存・読み戻し。
- 設定がない状態からの標準 Gateway 監視処理による復元、処理中・入力待ちでの延期、待機時の起動受付。
- 新規・キャッシュ済みターンでの実 Gateway コンテキスト設定、executor、通常のツール dispatch。
- 標準の圧縮用ロック・子セッション作成・Heartbeat 移行・ルート切替。ルート切替前の子セッションで解除しても、切替後に設定が復活しないこと。
- 不正な引数、保存データの破損、読み書き失敗、別ホーム・別会話・サブエージェント・停止中ルートの拒否と、拒否時の無関係データ不変。

時計・監視周期・メッセージングアダプター・処理中判定はテスト用に模擬しています。エージェントも必要な属性だけを持つテスト用オブジェクトです。実モデル呼び出し、完全な `run_conversation`、圧縮モデル、Telegram / Discord の実通信は実行していません。

過去の導入では、標準 enable 操作のホットリロード応答で Gateway のプラグイン登録と再起動不要を確認しました。Python ツールは新規セッションまで保留されました。**新規セッションでのモデル向けツール表示、実モデル実行、実際の定期起動とメッセージ配信は未検証**です。隔離テスト成功を本番の一連の動作成功とは扱いません。

## 内部 API への依存

登録には公開の `ctx.register_tool` を使いますが、操作には以下の Hermes 内部 API が必要です。

- `hermes_cli.heartbeat` の `HeartbeatManager`、`HeartbeatState`、`parse_interval`。
- `hermes_state_registry` と SessionDB の metadata、ルーティング情報、圧縮先取得。
- `gateway.session_context`、Gateway の platform 定義、delegation-context 判定。

これらの内部モジュールや保存 API は、一般プラグイン向けの安定した公開契約ではありません。ホストを更新するときは、対応するソースで再検証してください。標準 `$HERMES_HOME/sessions` 以外のルーティング保存先を探索する fallback は設けていません。

## 隔離検証の実行方法

Hermes のソース checkout と、その環境にインストール済みの依存パッケージ・`pytest` が必要です。`scripts/check.py` 自体は Python 標準ライブラリだけで起動します。プラグイン専用の外部 Python 依存はありません。スクリプトはパッケージの追加インストールを行いません。

リポジトリのルートで実行します。

```sh
# Hermes が標準の場所にある場合
HERMES_SOURCE="$HOME/.hermes/hermes-agent" python3 scripts/check.py
```

checkout が別の場所にある場合は `HERMES_SOURCE` を変更します。ホストの Python は既定で `$HERMES_SOURCE/venv/bin/python` を使います。managed runtime などで Python が別の場所にある場合は、実際にホスト依存を持つインタープリターの絶対パスを `HERMES_PYTHON` に指定してください。checkout の venv があれば、その site-packages も検証プロセスで追加します。ほかのインストール構成での実行は未確認です。

一時ファイルは `TMPDIR`、未指定なら `$HOME/.hermes/cache/scratch` に置きます。検証プロセスには最小限の環境変数だけを渡し、継承した認証情報や会話設定を使いません。Hermes の import 前に scratch の `HOME` / `HERMES_HOME` を設定し、lazy install を無効化し、live agent の import を拒否します。bootstrap の未使用ネットワーク関数は、呼ぶと失敗する代替関数に限定しています。

`check.py` は次を実行・生成します。

1. ソースのテストと、scratch に隔離した Plugin Doctor。
2. `dist/session-heartbeat-0.1.0.tar.gz` の生成。
3. 展開した配布物と元ファイルの byte 一致確認、配布物の同じテストと Doctor。
4. `evidence/` への実出力・ビルドハッシュ・ホスト Git リビジョンと worktree 状態の記録。

生成したログにはローカルパスやホスト状態が含まれるため、`evidence/` と `dist/` は Git ignore しています。公開 Issue に貼る場合も秘密値・会話 ID・個人情報を除去してください。テスト中の DB・設定・プラグインコピーは scratch に作り、終了時に削除します。導入、有効化、ホットリロード、サービス操作、本番 DB の操作は行いません。

## 配布物

GitHub からの標準導入は `plugin/` の native directory package を使います。生成する tar は別の検証用配布物で、次を `session-heartbeat/` 以下に含みます。

- `plugin.yaml`
- `__init__.py`
- `heartbeat_tool.py`
- `README.md`
- `LICENSE`
- `docs/verification.md`

tar 内の README はリポジトリへの案内も含みます。開発用のテスト・検証スクリプトは tar ではなく、リポジトリから取得してください。
