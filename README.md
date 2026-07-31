# Slack Reader MCP

Slack Reader MCP は、Slack の情報を**読み取り専用**で参照するローカル MCP サーバーです。FastMCP の stdio transport で動作し、Slack Web API の `assistant.search.context` による検索、`conversations.replies` によるスレッド取得、`conversations.list` によるチャンネル名からID解決を提供します。

## 前提条件

- Python 3.11 以上
- [uv](https://docs.astral.sh/uv/) がインストール済みであること
- Slack ワークスペースが Business+ / Enterprise+ プランであること
- Slack AI Search が有効で、`assistant.search.context` を利用できること
- `assistant.search.context` にはユーザーあたり約 10 リクエスト/分のレート制限があります。`ratelimited` の場合は `Retry-After` に従って待機してください。

## ⚠️ 読み取り専用ポリシー（最重要）

このリポジトリでは以下を厳守します。

- 書き込みスコープ（`chat:write` など）は追加禁止
- Slack へ投稿する MCP ツールの追加禁止
- **User Token のみ使用**し、Bot Token は発行・使用しない
- Bot Token Scopes は Slack App に追加しない
- HTTP クライアント層で呼び出し可能エンドポイントをホワイトリスト制限する多層防御を行う
  - 許可エンドポイント: `assistant.search.context`, `conversations.replies`, `conversations.list`, `users.info`
  - 上記以外の Slack API 呼び出しは例外になります（OAuth トークン交換は auth モジュールが直接行い、このホワイトリストには含めません）

## 成果物構成

主なファイルは次の通りです。

- `pyproject.toml`: uv プロジェクト設定、`slack-mcp-auth` / `slack-mcp-server` エントリポイント
- `.env.example`: `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_REDIRECT_URI` の例
- `.gitignore`: `.credentials/`, `.env` などの除外設定
- `src/slack_reader_mcp/auth.py`: OAuth 認可、User Token 保存・更新、認可 CLI
- `src/slack_reader_mcp/slack_client.py`: エンドポイントホワイトリスト付き Slack HTTP クライアント、ユーザー名解決キャッシュ
- `src/slack_reader_mcp/server.py`: FastMCP サーバー本体と読み取り専用ツール定義
- `README.md`: セットアップ、Slack App 設定、認可、MCP クライアント設定、トラブルシューティング

## Slack App 作成手順

1. <https://api.slack.com/apps> を開き、Slack App を作成します。
2. **OAuth & Permissions** を開きます。
3. **User Token Scopes** に次の読み取りスコープだけを追加します。
   - `search:read.public`
   - `search:read.private`
   - `search:read.im`
   - `search:read.mpim`
   - `channels:history`
   - `groups:history`
   - `im:history`
   - `mpim:history`
   - `channels:read`
   - `groups:read`
   - `users:read`

   `channels:read` / `groups:read` は `slack_resolve_channel_id` ツール（チャンネル名→ID解決）が使用する `conversations.list` に必要です。
4. **Bot Token Scopes は追加しないでください。**
5. **Redirect URLs** に次を登録します。

```text
https://localhost:8000/callback
```

Slack の Redirect URL は HTTPS 必須です。`http://localhost` は登録できません。このプロジェクトの認可コマンドは、起動時に自己署名証明書を生成して `https://localhost:8000/callback` で 1 回だけ OAuth コールバックを受け取ります。

## セットアップ

```bash
uv sync
cp .env.example .env
```

`.env` に Slack App の値を設定します。

```env
SLACK_CLIENT_ID=your-client-id
SLACK_CLIENT_SECRET=your-client-secret
SLACK_REDIRECT_URI=https://localhost:8000/callback
```

`SLACK_REDIRECT_URI` は Slack App の Redirect URLs に登録した値と完全に一致させてください。

## 認可

認可は MCP サーバー起動時ではなく、独立した CLI コマンドで先に実行します。

```bash
uv run slack-mcp-auth
```

実行すると Slack 認可 URL がブラウザで開きます。開かない場合は、ターミナルに表示された URL をコピーして開いてください。

### 自己署名証明書の警告を許可する

OAuth コールバックは `https://localhost:8000/callback` で受け取るため、ブラウザに自己署名証明書の警告が表示されます。ローカルホスト向けに一時生成された証明書なので、認可時だけ許可してください。

- Chrome: 「詳細設定」→「localhost にアクセスする（安全ではありません）」を選択
- Safari: 警告画面で詳細を表示し、Web サイトへのアクセスを続行
- Firefox: 「詳細情報」→「危険性を承知で続行」または例外を追加

認可が完了すると、User Token が次のファイルに保存されます。

```text
.credentials/credentials.json
```

POSIX 環境では `.credentials/` は `0700`、`credentials.json` は `0600` に設定されます。`.credentials/` は `.gitignore` 済みです。Slack App 側でトークンローテーションが有効な場合、`refresh_token` / `expires_in` が保存され、期限切れ時に自動リフレッシュされます。

## MCP クライアント設定例（stdio）

### Claude Desktop

設定ファイルの `mcpServers` に追加します。`/path/to/slack-reader` はこのリポジトリの絶対パスに置き換えてください。

```json
{
  "mcpServers": {
    "slack-reader": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/slack-reader", "slack-mcp-server"]
    }
  }
}
```

### VS Code

MCP サーバー設定に次のように追加します。

```json
{
  "servers": {
    "slack-reader": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/slack-reader", "slack-mcp-server"]
    }
  }
}
```

サーバーのエントリポイントは `pyproject.toml` の `slack-mcp-server = "slack_reader_mcp.server:main"` です。

## 提供ツール

いずれのツールも、人間が読みやすい整形済みテキスト（`content`）に加えて、
同じ内容を持つ構造化データ（`structuredContent`）を返します。AI クライアント
が permalink やタイムスタンプなどのフィールドを正確に扱いたい場合は
`structuredContent` を、要約して回答するだけの場合はテキストをそのまま利用
できます。取得や検索に失敗した場合は `isError=true` とエラーメッセージが返り
ます。

### `slack_search_context(query, count=10, cursor=None)`

Slack 内のメッセージと周辺文脈を `assistant.search.context` で検索します。

引数:

- `query` (`str`, 必須): 検索キーワード
- `count` (`int`, 任意): 取得件数。初期値は `10`、最大 `20`。コード上は `1` 未満を `1`、`20` 超を `20` に丸めます。
- `cursor` (`str | None`, 任意): 次ページ取得用カーソル

動作:

- `POST https://slack.com/api/assistant.search.context` を呼び出します。
- `query`, `limit`, 必要に応じて `cursor` を送信します。
- 検索結果のメッセージ、周辺文脈（`context_messages` / `context`）、permalink を整形して返します。
- Slack のユーザー ID や `<@U...>` メンションは `users.info` で表示名に解決します。
- レスポンスに次ページがある場合、結果末尾に `次ページ取得用cursor: ...` を表示します。次回呼び出し時にその値を `cursor` に指定してください。
- `structuredContent` は次の形（`SearchContextOutput`）です。

  ```json
  {
    "results": [
      {
        "ts": "1700000000.000100",
        "timestamp": "2023-11-15T07:13:20+09:00",
        "author": "Alice",
        "channel_id": "C1",
        "text": "本文",
        "permalink": "https://.../p1",
        "context_messages": [
          {"ts": "...", "timestamp": "...", "author": "...", "text": "..."}
        ]
      }
    ],
    "next_cursor": "abc"
  }
  ```

### `slack_get_thread_from_url(slack_url)`

Slack のメッセージ URL またはスレッド URL からスレッド全文を取得します。

引数:

- `slack_url` (`str`, 必須): Slack のメッセージまたはスレッド URL

動作:

- URL の `/archives/{channel_id}/p{timestamp}` から `channel_id` と `ts` を抽出します。
- URL に `thread_ts` クエリパラメータがある場合は、それを親スレッドの `ts` として優先します。
- `p1600000000123456` 形式の permalink timestamp は `1600000000.123456` に変換します。
- `GET https://slack.com/api/conversations.replies` を `channel`, `ts`, `limit=200` で呼び出します。
- `response_metadata.next_cursor` が空でなくなるまで `cursor` を指定してページネーションし、全メッセージを取得します（想定外の応答で無限ループしないよう、最大100ページで打ち切ります）。
- 親メッセージと返信を時系列に並べ、送信者、ローカルタイムゾーンの時刻、本文を整形して返します。
- `structuredContent` は次の形（`ThreadOutput`）です。

  ```json
  {
    "channel_id": "C123ABC",
    "reply_count": 1,
    "messages": [
      {"ts": "...", "timestamp": "...", "author": "...", "text": "..."}
    ]
  }
  ```

### `slack_resolve_channel_id(channel_name, include_archived=False)`

チャンネル名からチャンネルIDを解決します。`conversations.replies` などのAPIはチャンネル名ではなくIDを要求するため、ユーザーがチャンネル名だけを指定した場合に事前解決するためのツールです。

引数:

- `channel_name` (`str`, 必須): チャンネル名。先頭の `#` は付けても付けなくても可（例: `"general"` または `"#general"`）
- `include_archived` (`bool`, 任意): アーカイブ済みチャンネルも検索対象に含めるか。初期値は `False`

動作:

- `GET https://slack.com/api/conversations.list` を `types=public_channel,private_channel` で呼び出し、`cursor` を使って全ページを走査します（最大50ページ）。
- チャンネル名の完全一致（大文字小文字を区別しない）を探し、最初に見つかったチャンネルIDを返します。
- DM・グループDMは検索対象外です。パブリック／プライベートチャンネルのみが対象で、トークンでアクセス可能な範囲に限られます。
- 見つからない場合はエラーにはせず、`found: false` を返します。
- `structuredContent` は次の形（`ChannelIdOutput`）です。

  ```json
  {
    "channel_name": "general",
    "channel_id": "C123ABC",
    "found": true
  }
  ```

## トラブルシューティング

### 「先に認可コマンドを実行してください」またはトークンが見つからない

先に次を実行してください。

```bash
uv run slack-mcp-auth
```

認可後、`.credentials/credentials.json` が作成されていることを確認します。

### `missing_scope`

Slack App の **User Token Scopes** に必要な読み取りスコープが不足しています。スコープを追加した後、再度認可してください。

```bash
uv run slack-mcp-auth
```

### `ratelimited`

Slack API のレート制限です。`assistant.search.context` は約 10 リクエスト/分/ユーザーに制限されています。`Retry-After` に従って待機し、検索回数を減らしてください。

### 証明書警告で認可が進まない

`https://localhost:8000/callback` の自己署名証明書警告を許可してください。Chrome は「詳細設定」→「localhost にアクセスする（安全ではありません）」、Safari / Firefox も詳細表示から続行または例外追加を選びます。

### Slack AI Search が無効

`assistant.search.context` は Slack AI Search が有効な Business+ / Enterprise+ ワークスペースで利用できます。プランと Slack AI Search の有効化状況を確認してください。

### `not_allowed_token_type`

Bot Token など、許可されていないトークン種別です。Bot Token は使わず、User Token (`xoxp-`) で再認可してください。

### `channel_not_found` / `thread_not_found`

URL、チャンネルへのアクセス権、対象スレッドの存在を確認してください。プライベートチャンネルや DM は、認可したユーザーがアクセスできる範囲のみ取得できます。

## 開発

テストは次で実行します。

```bash
uv run pytest
```

stdio transport では stdout が MCP プロトコル専用です。ログは stderr に出力されます。
