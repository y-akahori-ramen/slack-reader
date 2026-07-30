"""Slack Web API client for the read-only MCP server.

読み取り専用ポリシー: 書き込みスコープ・投稿ツールの追加禁止。
AI による Slack への投稿は禁止されているため、このモジュールは許可された
読み取り系 API だけを呼び出せるよう低レベルリクエストでホワイトリスト検査します。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SLACK_API_BASE_URL = "https://slack.com/api"
ALLOWED_ENDPOINTS = frozenset(
    {
        "assistant.search.context",
        "conversations.replies",
        "users.info",
        "oauth.v2.access",
    }
)
MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")


class SlackClientError(Exception):
    """Base exception for Slack client errors."""


class EndpointNotAllowedError(SlackClientError):
    """Raised when code attempts to call an API method outside the whitelist."""


class SlackAPIError(SlackClientError):
    """Raised for Slack Web API errors with user-actionable guidance."""

    def __init__(self, error_code: str, message: str, *, response: Mapping[str, Any] | None = None) -> None:
        self.error_code = error_code
        self.response = response
        super().__init__(message)


class SlackClient:
    """Synchronous, read-only Slack Web API client.

    Args:
        token_provider: Callable returning a valid xoxp- user token. Missing-token
            guidance is intentionally handled by the auth module.
        timeout: HTTP timeout in seconds.
        max_retries: Number of rate-limit retries before raising SlackAPIError.
        max_retry_after: Maximum seconds to sleep for a Slack Retry-After value.
    """

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        max_retry_after: float = 60.0,
    ) -> None:
        self._token_provider = token_provider
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._max_retry_after = max(0.0, max_retry_after)
        self._user_cache: dict[str, str] = {}

    def search_context(self, query: str, count: int = 10, cursor: str | None = None) -> dict[str, Any]:
        """Search Slack context using assistant.search.context."""
        limit = min(max(count, 1), 20)
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if cursor:
            payload["cursor"] = cursor
        return self._request("POST", "assistant.search.context", json=payload)

    def conversations_replies(
        self,
        channel: str,
        ts: str,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Fetch replies for a Slack conversation thread."""
        params: dict[str, Any] = {"channel": channel, "ts": ts, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "conversations.replies", params=params)

    def users_info(self, user: str) -> dict[str, Any]:
        """Fetch Slack user information."""
        return self._request("GET", "users.info", params={"user": user})

    def resolve_user(self, user_id: str) -> str:
        """Resolve a Slack user ID to a display name, returning the raw ID on failure."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        try:
            data = self.users_info(user_id)
            user = data.get("user") or {}
            profile = user.get("profile") or {}
            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            display_name = str(display_name)
            self._user_cache[user_id] = display_name
            return display_name
        except Exception as exc:  # noqa: BLE001 - mention resolution must not break tool output.
            logger.debug("Failed to resolve Slack user %s: %s", user_id, exc)
            return user_id

    def resolve_mentions(self, text: str) -> str:
        """Replace Slack <@U...> mentions with @display-name strings."""

        def replace(match: re.Match[str]) -> str:
            return f"@{self.resolve_user(match.group(1))}"

        return MENTION_RE.sub(replace, text)

    def _request(
        self,
        http_method: str,
        api_method: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a whitelisted Slack API method.

        書き込みスコープ・投稿ツールの追加禁止。防御層として、この低レベル
        メソッドで許可 API 以外を必ず拒否し、上位コードからの迂回を防ぎます。
        assistant.search.context はユーザーあたり約 10 req/min の制限があるため、
        レート制限時は Retry-After を尊重して限定回数だけ再試行します。
        """
        if api_method not in ALLOWED_ENDPOINTS:
            raise EndpointNotAllowedError(f"Slack API method is not allowed: {api_method}")

        url = f"{SLACK_API_BASE_URL}/{api_method}"
        headers = {"Accept": "application/json"}
        if api_method != "oauth.v2.access":
            headers["Authorization"] = f"Bearer {self._token_provider()}"

        for attempt in range(self._max_retries + 1):
            response = httpx.request(
                http_method,
                url,
                headers=headers,
                params=params,
                json=json,
                timeout=self._timeout,
            )

            if response.status_code == 429:
                if attempt < self._max_retries:
                    self._sleep_for_retry_after(response.headers.get("Retry-After"))
                    continue
                raise SlackAPIError("ratelimited", self._error_message("ratelimited"))

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SlackAPIError(
                    f"http_{response.status_code}",
                    f"Slack API HTTP エラー ({response.status_code}) が発生しました。時間をおいて再試行してください。",
                ) from exc

            data = response.json()
            if not isinstance(data, dict):
                raise SlackAPIError("invalid_response", "Slack API から不正なレスポンスを受信しました。")

            if data.get("ok", False):
                return data

            error_code = str(data.get("error") or "unknown_error")
            if error_code == "ratelimited" and attempt < self._max_retries:
                self._sleep_for_retry_after(response.headers.get("Retry-After"))
                continue
            raise SlackAPIError(error_code, self._error_message(error_code), response=data)

        raise SlackAPIError("ratelimited", self._error_message("ratelimited"))

    def _sleep_for_retry_after(self, retry_after_header: str | None) -> None:
        retry_after = self._parse_retry_after(retry_after_header)
        logger.info("Slack API rate limited; retrying after %.1f seconds", retry_after)
        time.sleep(retry_after)

    def _parse_retry_after(self, retry_after_header: str | None) -> float:
        if retry_after_header is None:
            return min(1.0, self._max_retry_after)
        try:
            retry_after = float(retry_after_header)
        except ValueError:
            retry_after = 1.0
        return min(max(retry_after, 0.0), self._max_retry_after)

    @staticmethod
    def _error_message(error_code: str) -> str:
        messages = {
            "missing_scope": "Slack API のスコープが不足しています。Slack App 設定で必要な読み取りスコープを追加し、再認可してください。",
            "not_allowed_token_type": "この API では現在のトークン種別を使用できません。User Token (xoxp-) で再認可してください。",
            "invalid_auth": "Slack 認証が無効です。uv run slack-mcp-auth を実行して再認可してください。",
            "token_expired": "Slack トークンの有効期限が切れています。uv run slack-mcp-auth を実行して再認可してください。",
            "token_revoked": "Slack トークンが取り消されています。uv run slack-mcp-auth を実行して再認可してください。",
            "ratelimited": "Slack API のレート制限に達しました。Retry-After に従って待機してから再試行してください。",
            "channel_not_found": "指定されたチャンネルが見つからないか、アクセス権がありません。URL と権限を確認してください。",
            "thread_not_found": "指定されたスレッドが見つかりません。Slack URL または thread_ts を確認してください。",
        }
        return messages.get(error_code, f"Slack API エラーが発生しました (error={error_code})。設定と権限を確認してください。")
