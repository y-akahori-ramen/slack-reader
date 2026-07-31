"""FastMCP server exposing read-only Slack tools.

読み取り専用ポリシー: 書き込みスコープ・投稿ツールの追加禁止。
AI による Slack への投稿は禁止されているため、このサーバーには検索と
スレッド取得の読み取り専用ツールだけを定義します。
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field

from slack_reader_mcp.auth import AuthRequiredError, get_access_token
from slack_reader_mcp.slack_client import (
    EndpointNotAllowedError,
    SlackAPIError,
    SlackClient,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

mcp = FastMCP("slack-reader")
_client: SlackClient | None = None

_ARCHIVES_RE = re.compile(r"/archives/([^/?#]+)/")
_PERMALINK_TS_RE = re.compile(r"/p(\d{10,})(?:[/?#]|$)")
# conversations.replies is paginated with limit=200; this caps worst-case
# iterations (100 pages = up to 20,000 messages) so an unexpected cursor loop
# from the Slack API cannot hang the tool call indefinitely.
_MAX_THREAD_PAGES = 100


def _get_client() -> SlackClient:
    """Create the shared Slack client lazily so auth errors occur per tool call."""
    global _client
    if _client is None:
        _client = SlackClient(get_access_token)
    return _client


def slack_ts_to_local_iso(ts: str | int | float | None) -> str:
    """Slack ts をローカルタイムゾーンの ISO 形式に変換します。"""
    if ts is None:
        return "時刻不明"
    try:
        timestamp = float(str(ts))
    except (TypeError, ValueError):
        return str(ts)
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def parse_slack_url(slack_url: str) -> tuple[str, str]:
    """Return (channel_id, thread_ts/message_ts) extracted from a Slack message URL."""
    parsed = urlparse(slack_url)
    channel_match = _ARCHIVES_RE.search(parsed.path)
    permalink_ts_match = _PERMALINK_TS_RE.search(parsed.path)
    if not channel_match or not permalink_ts_match:
        raise ValueError(
            "Slack URL からチャンネルIDまたはメッセージtsを取得できませんでした。"
        )

    channel_id = channel_match.group(1)
    query = parse_qs(parsed.query)
    thread_ts = query.get("thread_ts", [""])[0]
    if thread_ts:
        return channel_id, thread_ts

    digits = permalink_ts_match.group(1)
    if len(digits) <= 6:
        raise ValueError("Slack URL のメッセージts形式が不正です。")
    return channel_id, f"{digits[:-6]}.{digits[-6:]}"


def _stringify_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _message_author_id(message: dict[str, Any]) -> str:
    return _stringify_text(
        message.get("author")
        or message.get("author_id")
        or message.get("user")
        or message.get("user_id")
        or message.get("bot_id")
        or "unknown"
    )


def _message_ts(message: dict[str, Any]) -> str:
    return _stringify_text(
        message.get("message_ts") or message.get("ts") or message.get("timestamp")
    )


def _message_text(message: dict[str, Any]) -> str:
    return _stringify_text(
        message.get("content") or message.get("text") or message.get("message") or ""
    )


def _message_sort_key(message: dict[str, Any]) -> float:
    try:
        return float(_message_ts(message) or 0)
    except ValueError:
        return 0.0


def _next_cursor(response: dict[str, Any]) -> str:
    metadata = response.get("response_metadata") or {}
    cursor = ""
    if isinstance(metadata, dict):
        cursor = _stringify_text(metadata.get("next_cursor"))
    return cursor or _stringify_text(response.get("next_cursor"))


class MessageOutput(BaseModel):
    """1件のSlackメッセージを表す構造化データ。"""

    ts: str = Field(description="Slackメッセージのタイムスタンプ(ts)")
    timestamp: str = Field(description="ローカルタイムゾーンのISO 8601形式の日時")
    author: str = Field(description="投稿者の表示名（解決できない場合はID）")
    text: str = Field(description="メンション解決済みの本文")


class SearchResultOutput(BaseModel):
    """検索結果1件分の構造化データ。"""

    ts: str = Field(description="Slackメッセージのタイムスタンプ(ts)")
    timestamp: str = Field(description="ローカルタイムゾーンのISO 8601形式の日時")
    author: str = Field(description="投稿者の表示名（解決できない場合はID）")
    channel_id: str = Field(description="投稿されたチャンネルのID")
    text: str = Field(description="メンション解決済みの本文")
    permalink: str = Field(default="", description="メッセージへのpermalink URL")
    context_messages: list[MessageOutput] = Field(
        default_factory=list, description="前後の文脈メッセージ"
    )


class SearchContextOutput(BaseModel):
    """slack_search_contextツールの構造化された戻り値。"""

    results: list[SearchResultOutput] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None, description="次ページ取得用cursor。次ページが無い場合はnull"
    )


class ThreadOutput(BaseModel):
    """slack_get_thread_from_urlツールの構造化された戻り値。"""

    channel_id: str = Field(description="チャンネルID")
    reply_count: int = Field(description="親メッセージを除いた返信件数")
    messages: list[MessageOutput] = Field(
        default_factory=list, description="親メッセージを含む時系列順のメッセージ一覧"
    )


class ChannelIdOutput(BaseModel):
    """slack_resolve_channel_idツールの構造化された戻り値。"""

    channel_name: str = Field(
        description="検索に使用したチャンネル名（先頭の#は除去済み）"
    )
    channel_id: str | None = Field(
        default=None, description="解決されたチャンネルID。見つからない場合はnull"
    )
    found: bool = Field(description="チャンネルが見つかったかどうか")


def _build_message_output(
    client: SlackClient, message: dict[str, Any]
) -> MessageOutput:
    ts = _message_ts(message)
    return MessageOutput(
        ts=ts,
        timestamp=slack_ts_to_local_iso(ts),
        author=client.resolve_user(_message_author_id(message)),
        text=client.resolve_mentions(_message_text(message)) or "(本文なし)",
    )


def _build_search_context_output(
    client: SlackClient, response: dict[str, Any]
) -> SearchContextOutput:
    results = response.get("results") or {}
    messages: Any = []
    if isinstance(results, dict):
        messages = results.get("messages") or []
    if not messages and isinstance(response.get("messages"), list):
        messages = response.get("messages") or []

    search_results: list[SearchResultOutput] = []
    if isinstance(messages, list):
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                continue
            channel_id = _stringify_text(
                raw_message.get("channel_id") or raw_message.get("channel") or "unknown"
            )
            permalink = _stringify_text(
                raw_message.get("permalink") or raw_message.get("url")
            )
            context_messages = (
                raw_message.get("context_messages") or raw_message.get("context") or []
            )
            context_output = [
                _build_message_output(client, context_message)
                for context_message in context_messages
                if isinstance(context_message, dict)
            ]
            result_ts = _message_ts(raw_message)
            search_results.append(
                SearchResultOutput(
                    ts=result_ts,
                    timestamp=slack_ts_to_local_iso(result_ts),
                    author=client.resolve_user(_message_author_id(raw_message)),
                    channel_id=channel_id,
                    text=client.resolve_mentions(_message_text(raw_message))
                    or "(本文なし)",
                    permalink=permalink,
                    context_messages=context_output,
                )
            )

    cursor = _next_cursor(response)
    return SearchContextOutput(results=search_results, next_cursor=cursor or None)


def _render_search_text(data: SearchContextOutput) -> str:
    if not data.results:
        lines = ["検索結果はありませんでした。"]
    else:
        lines = []
        for index, result in enumerate(data.results, start=1):
            lines.append(
                f"### {index}. [{result.timestamp}] "
                f"{result.author} / {result.channel_id}"
            )
            lines.append(result.text)
            if result.permalink:
                lines.append(f"permalink: {result.permalink}")
            if result.context_messages:
                lines.append("context_messages:")
                for context_message in result.context_messages:
                    lines.append(
                        f"  - [{context_message.timestamp}] "
                        f"{context_message.author}: {context_message.text}"
                    )
            lines.append("")

    if data.next_cursor:
        lines.append(f"次ページ取得用cursor: {data.next_cursor}")
    return "\n".join(lines).rstrip()


def _build_thread_output(
    client: SlackClient, channel_id: str, messages: list[dict[str, Any]]
) -> ThreadOutput:
    sorted_messages = sorted(messages, key=_message_sort_key)
    reply_count = max(len(sorted_messages) - 1, 0)
    return ThreadOutput(
        channel_id=channel_id,
        reply_count=reply_count,
        messages=[
            _build_message_output(client, message) for message in sorted_messages
        ],
    )


def _render_thread_text(data: ThreadOutput) -> str:
    lines = [
        f"チャンネル: {data.channel_id}",
        f"メッセージ数: {len(data.messages)}（返信 {data.reply_count} 件）",
        "",
    ]
    for message in data.messages:
        lines.append(f"[{message.timestamp}] {message.author}: {message.text}")
    return "\n".join(lines).rstrip()


def _tool_error_message(exc: Exception) -> str:
    if isinstance(
        exc, (AuthRequiredError, SlackAPIError, EndpointNotAllowedError, ValueError)
    ):
        return str(exc)
    logger.exception("Unexpected Slack MCP server error")
    return f"Slack MCPサーバーで予期しないエラーが発生しました: {exc}"


@mcp.tool(output_schema=SearchContextOutput.model_json_schema())
def slack_search_context(
    query: str, count: int = 10, cursor: str | None = None
) -> ToolResult:
    """Slack内のメッセージとその前後の文脈を読み取り専用で検索します。

    ユーザーが特定の話題やキーワードについて話しているメッセージ・スレッドを
    探したいときに使用します。Slack標準の検索構文（例: `in:#channel`、
    `from:@user`、`before:2024-01-01`、`after:2024-01-01`）がqueryにそのまま
    使用できます。各結果には投稿者・日時・本文に加えて前後の文脈メッセージと
    permalinkが含まれます。

    Args:
        query: Slack検索クエリ文字列（例: "in:#general デプロイ手順"）。
        count: 取得する検索結果の最大件数。1〜20の範囲にクランプされます
            （デフォルト10）。
        cursor: 前回のレスポンス末尾に含まれる「次ページ取得用cursor」の値。
            2ページ目以降を取得する場合にのみ指定し、初回は省略します。

    Returns:
        人間が読みやすい整形済みテキストに加えて、同じ内容を持つ
        structuredContent（`SearchContextOutput`）を返します。
        構造化データには各結果のts・timestamp・author・channel_id・text・
        permalink・context_messagesと、次ページ用のnext_cursorが含まれます。
        取得や検索に失敗した場合はisError=trueとエラーメッセージを返します。
    """
    try:
        client = _get_client()
        response = client.search_context(query, count=count, cursor=cursor)
        data = _build_search_context_output(client, response)
        return ToolResult(content=_render_search_text(data), structured_content=data)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - unexpected errors are still returned as tool results.
        return ToolResult(content=_tool_error_message(exc), is_error=True)


@mcp.tool(output_schema=ThreadOutput.model_json_schema())
def slack_get_thread_from_url(slack_url: str) -> ToolResult:
    """Slackメッセージ／スレッドのURLから、スレッド全文を読み取り専用で取得します。

    ユーザーがSlackのパーマリンク（メッセージ右クリック→「リンクをコピー」で
    得られるURL）を提示し、そのスレッドの親メッセージと全返信を時系列で読みたい
    ときに使用します。返信が多いスレッドでもページネーションして全件取得します。

    Args:
        slack_url: Slackメッセージのパーマリンク。
            `https://<workspace>.slack.com/archives/<channel_id>/p<digits>` の
            形式のパスを含み、スレッド内返信のURLの場合は
            `?thread_ts=<ts>` クエリパラメータを含むことがあります。

    Returns:
        人間が読みやすい整形済みテキストに加えて、同じ内容を持つ
        structuredContent（`ThreadOutput`）を返します。構造化データには
        channel_id・reply_count・時系列順のmessages（ts・timestamp・author・
        text）が含まれます。URLの解析やメッセージ取得に失敗した場合は
        isError=trueとエラーメッセージを返します。
    """
    try:
        channel_id, ts = parse_slack_url(slack_url)
        client = _get_client()
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_THREAD_PAGES):
            response = client.conversations_replies(
                channel_id, ts, cursor=cursor, limit=200
            )
            raw_messages = response.get("messages") or []
            if isinstance(raw_messages, list):
                messages.extend(
                    message for message in raw_messages if isinstance(message, dict)
                )
            cursor = _next_cursor(response)
            if not cursor:
                break
        else:
            logger.warning(
                "slack_get_thread_from_url: reached the %d page pagination limit "
                "for channel=%s ts=%s; returning partial results.",
                _MAX_THREAD_PAGES,
                channel_id,
                ts,
            )
        data = _build_thread_output(client, channel_id, messages)
        return ToolResult(content=_render_thread_text(data), structured_content=data)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - unexpected errors are still returned as tool results.
        return ToolResult(content=_tool_error_message(exc), is_error=True)


@mcp.tool(output_schema=ChannelIdOutput.model_json_schema())
def slack_resolve_channel_id(
    channel_name: str, include_archived: bool = False
) -> ToolResult:
    """チャンネル名からチャンネルIDを読み取り専用で解決します。

    Slackのメッセージ取得系ツール（`conversations.replies` 等）はチャンネル名
    ではなくチャンネルIDを要求します。ユーザーが `#general` や `general` の
    ようにチャンネル名だけを指定した場合、まずこのツールでチャンネルIDに
    変換してから他のツールに渡してください。ワークスペース内の
    パブリック／プライベートチャンネルを対象に、完全一致（大文字小文字を
    区別しない）で検索します。DM・グループDMは対象外です。

    Args:
        channel_name: 検索するチャンネル名。先頭の `#` は付けても付けなくても
            構いません（例: "general" または "#general"）。
        include_archived: アーカイブ済みチャンネルも検索対象に含めるか。
            デフォルトは含めない(False)。

    Returns:
        人間が読みやすい整形済みテキストに加えて、同じ内容を持つ
        structuredContent（`ChannelIdOutput`）を返します。構造化データには
        channel_name・channel_id（見つからない場合はnull）・foundが含まれます。
        該当するチャンネルが無い場合もエラーにはせず found=false を返します。
        API呼び出し自体に失敗した場合（権限不足など）はisError=trueと
        エラーメッセージを返します。
    """
    try:
        normalized_name = channel_name.removeprefix("#").strip()
        client = _get_client()
        channel_id = client.find_channel_id_by_name(
            normalized_name, include_archived=include_archived
        )
        data = ChannelIdOutput(
            channel_name=normalized_name,
            channel_id=channel_id,
            found=channel_id is not None,
        )
        if data.found:
            text = f"チャンネル「{normalized_name}」のIDは {channel_id} です。"
        else:
            text = (
                f"チャンネル「{normalized_name}」が見つかりませんでした。"
                "名前を確認するか、アクセス権のあるチャンネルか確認してください。"
            )
        return ToolResult(content=text, structured_content=data)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - unexpected errors are still returned as tool results.
        return ToolResult(content=_tool_error_message(exc), is_error=True)


def main() -> None:
    """Run the FastMCP stdio server."""
    mcp.run()


__all__ = [
    "ChannelIdOutput",
    "MessageOutput",
    "SearchContextOutput",
    "SearchResultOutput",
    "ThreadOutput",
    "main",
    "mcp",
    "parse_slack_url",
    "slack_get_thread_from_url",
    "slack_resolve_channel_id",
    "slack_search_context",
    "slack_ts_to_local_iso",
]
