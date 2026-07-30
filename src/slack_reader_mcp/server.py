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

from slack_reader_mcp.auth import AuthRequiredError, get_access_token
from slack_reader_mcp.slack_client import EndpointNotAllowedError, SlackAPIError, SlackClient

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
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_slack_url(slack_url: str) -> tuple[str, str]:
    """Return (channel_id, thread_ts/message_ts) extracted from a Slack message URL."""
    parsed = urlparse(slack_url)
    channel_match = _ARCHIVES_RE.search(parsed.path)
    permalink_ts_match = _PERMALINK_TS_RE.search(parsed.path)
    if not channel_match or not permalink_ts_match:
        raise ValueError("Slack URL からチャンネルIDまたはメッセージtsを取得できませんでした。")

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
    return _stringify_text(message.get("message_ts") or message.get("ts") or message.get("timestamp"))


def _message_text(message: dict[str, Any]) -> str:
    return _stringify_text(message.get("content") or message.get("text") or message.get("message") or "")


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


def _format_context_message(client: SlackClient, message: dict[str, Any]) -> str:
    author = client.resolve_user(_message_author_id(message))
    ts = slack_ts_to_local_iso(_message_ts(message))
    text = client.resolve_mentions(_message_text(message)) or "(本文なし)"
    return f"  - [{ts}] {author}: {text}"


def _format_search_results(client: SlackClient, response: dict[str, Any]) -> str:
    results = response.get("results") or {}
    messages: Any = []
    if isinstance(results, dict):
        messages = results.get("messages") or []
    if not messages and isinstance(response.get("messages"), list):
        messages = response.get("messages") or []
    if not isinstance(messages, list) or not messages:
        lines = ["検索結果はありませんでした。"]
    else:
        lines = []
        for index, raw_message in enumerate(messages, start=1):
            if not isinstance(raw_message, dict):
                continue
            author = client.resolve_user(_message_author_id(raw_message))
            ts = slack_ts_to_local_iso(_message_ts(raw_message))
            channel_id = _stringify_text(raw_message.get("channel_id") or raw_message.get("channel") or "unknown")
            text = client.resolve_mentions(_message_text(raw_message)) or "(本文なし)"
            permalink = _stringify_text(raw_message.get("permalink") or raw_message.get("url"))

            lines.append(f"### {index}. [{ts}] {author} / {channel_id}")
            lines.append(text)
            if permalink:
                lines.append(f"permalink: {permalink}")

            context_messages = raw_message.get("context_messages") or raw_message.get("context") or []
            if isinstance(context_messages, list) and context_messages:
                lines.append("context_messages:")
                for context_message in context_messages:
                    if isinstance(context_message, dict):
                        lines.append(_format_context_message(client, context_message))
                    else:
                        lines.append(f"  - {client.resolve_mentions(_stringify_text(context_message))}")
            lines.append("")
        if not lines:
            lines = ["検索結果はありませんでした。"]

    cursor = _next_cursor(response)
    if cursor:
        lines.append(f"次ページ取得用cursor: {cursor}")
    return "\n".join(lines).rstrip()


def _format_thread(client: SlackClient, channel_id: str, messages: list[dict[str, Any]]) -> str:
    sorted_messages = sorted(messages, key=_message_sort_key)
    reply_count = max(len(sorted_messages) - 1, 0)
    lines = [f"チャンネル: {channel_id}", f"メッセージ数: {len(sorted_messages)}（返信 {reply_count} 件）", ""]
    for message in sorted_messages:
        author = client.resolve_user(_message_author_id(message))
        ts = slack_ts_to_local_iso(_message_ts(message))
        text = client.resolve_mentions(_message_text(message)) or "(本文なし)"
        lines.append(f"[{ts}] {author}: {text}")
    return "\n".join(lines).rstrip()


def _tool_error_message(exc: Exception) -> str:
    if isinstance(exc, (AuthRequiredError, SlackAPIError, EndpointNotAllowedError, ValueError)):
        return str(exc)
    logger.exception("Unexpected Slack MCP server error")
    return f"Slack MCPサーバーで予期しないエラーが発生しました: {exc}"


@mcp.tool
def slack_search_context(query: str, count: int = 10, cursor: str | None = None) -> str:
    """Slack内のメッセージと周辺文脈を読み取り専用で検索します。"""
    try:
        client = _get_client()
        response = client.search_context(query, count=count, cursor=cursor)
        return _format_search_results(client, response)
    except Exception as exc:  # noqa: BLE001 - MCP tool results should be readable strings.
        return _tool_error_message(exc)


@mcp.tool
def slack_get_thread_from_url(slack_url: str) -> str:
    """Slack URLからスレッド全文を読み取り専用で取得します。"""
    try:
        channel_id, ts = parse_slack_url(slack_url)
        client = _get_client()
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = client.conversations_replies(channel_id, ts, cursor=cursor, limit=200)
            raw_messages = response.get("messages") or []
            if isinstance(raw_messages, list):
                messages.extend(message for message in raw_messages if isinstance(message, dict))
            cursor = _next_cursor(response)
            if not cursor:
                break
        if not messages:
            return "スレッド内のメッセージは見つかりませんでした。"
        return _format_thread(client, channel_id, messages)
    except Exception as exc:  # noqa: BLE001 - MCP tool results should be readable strings.
        return _tool_error_message(exc)


def main() -> None:
    """Run the FastMCP stdio server."""
    mcp.run()


__all__ = [
    "main",
    "mcp",
    "parse_slack_url",
    "slack_get_thread_from_url",
    "slack_search_context",
    "slack_ts_to_local_iso",
]
