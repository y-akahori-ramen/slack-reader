from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from slack_reader_mcp import auth, server
from slack_reader_mcp.auth import AuthRequiredError
from slack_reader_mcp.slack_client import (
    EndpointNotAllowedError,
    SlackAPIError,
    SlackClient,
)


def slack_response(status_code: int = 200, **kwargs: object) -> httpx.Response:
    request = httpx.Request("GET", "https://slack.com/api/test")
    return httpx.Response(status_code, request=request, **kwargs)


def test_parse_slack_url_converts_permalink_ts_for_archive_channel_ids() -> None:
    for channel_id in ("C123ABC", "G123ABC", "D123ABC"):
        parsed_channel, ts = server.parse_slack_url(
            f"https://example.slack.com/archives/{channel_id}/p1600000000123456"
        )
        assert parsed_channel == channel_id
        assert ts == "1600000000.123456"


def test_parse_slack_url_prefers_thread_ts_query_param() -> None:
    channel, ts = server.parse_slack_url(
        "https://example.slack.com/archives/C123ABC/p1600000000123456"
        "?thread_ts=1599999999.000200&cid=C123ABC"
    )

    assert channel == "C123ABC"
    assert ts == "1599999999.000200"


@pytest.mark.parametrize(
    "slack_url",
    [
        "not a url",
        "https://example.slack.com/archives/C123ABC/",
        "https://example.slack.com/messages/C123ABC/p1600000000123456",
        "https://example.slack.com/archives/C123ABC/p123456",
    ],
)
def test_parse_slack_url_rejects_invalid_urls(slack_url: str) -> None:
    with pytest.raises(ValueError):
        server.parse_slack_url(slack_url)


def test_slack_ts_to_local_iso_formats_valid_timestamp() -> None:
    expected = (
        datetime.fromtimestamp(1600000000.123456, tz=timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )

    assert server.slack_ts_to_local_iso("1600000000.123456") == expected


def test_endpoint_whitelist_blocks_non_readonly_method_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError(
            "HTTP request should not be sent for a disallowed endpoint"
        )

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fail_request)
    client = SlackClient(lambda: "xoxp-test")

    with pytest.raises(EndpointNotAllowedError):
        client._request(
            "POST", "chat.postMessage", json={"channel": "C1", "text": "blocked"}
        )


def test_resolve_mentions_replaces_plain_and_labeled_mentions() -> None:
    client = SlackClient(lambda: "xoxp-test")
    client.users_info = lambda user: {  # type: ignore[method-assign]
        "ok": True,
        "user": {"profile": {"display_name": f"name-{user}"}},
    }

    assert (
        client.resolve_mentions("Hi <@U123> and <@U456|fallback>")
        == "Hi @name-U123 and @name-U456"
    )


def test_resolve_user_caches_users_info_result() -> None:
    client = SlackClient(lambda: "xoxp-test")
    calls: list[str] = []

    def users_info(user: str) -> dict[str, object]:
        calls.append(user)
        return {"ok": True, "user": {"profile": {"display_name": "Alice"}}}

    client.users_info = users_info  # type: ignore[method-assign]

    assert client.resolve_user("U123") == "Alice"
    assert client.resolve_user("U123") == "Alice"
    assert calls == ["U123"]


def test_resolve_user_caches_failures_to_avoid_repeated_api_calls() -> None:
    client = SlackClient(lambda: "xoxp-test")
    calls: list[str] = []

    def users_info(user: str) -> dict[str, object]:
        calls.append(user)
        raise SlackAPIError("user_not_found", "user not found")

    client.users_info = users_info  # type: ignore[method-assign]

    # bot_id (B...) など users.info で解決できないIDは失敗もキャッシュされ、
    # 同一IDに対してAPIを繰り返し呼ばない。
    assert client.resolve_user("B123") == "B123"
    assert client.resolve_user("B123") == "B123"
    assert calls == ["B123"]


@pytest.mark.parametrize(
    ("error_code", "expected_guidance"),
    [
        ("missing_scope", "スコープ"),
        ("invalid_auth", "再認可"),
        ("ratelimited", "レート制限"),
    ],
)
def test_slack_api_error_messages_include_japanese_guidance(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_guidance: str,
) -> None:
    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return slack_response(json={"ok": False, "error": error_code})

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fake_request)
    client = SlackClient(lambda: "xoxp-test", max_retries=0)

    with pytest.raises(SlackAPIError) as exc_info:
        client.users_info("U123")

    assert exc_info.value.error_code == error_code
    assert expected_guidance in str(exc_info.value)


def test_rate_limit_http_status_raises_guidance_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return slack_response(429, headers={"Retry-After": "10"})

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fake_request)
    monkeypatch.setattr(
        "slack_reader_mcp.slack_client.time.sleep", lambda seconds: None
    )
    client = SlackClient(lambda: "xoxp-test", max_retries=0)

    with pytest.raises(SlackAPIError) as exc_info:
        client.search_context("hello")

    assert exc_info.value.error_code == "ratelimited"
    assert "Retry-After" in str(exc_info.value)


def test_search_context_clamps_count_to_twenty(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, object]] = []

    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        payloads.append(dict(kwargs["json"]))
        return slack_response(json={"ok": True, "results": {"messages": []}})

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fake_request)
    client = SlackClient(lambda: "xoxp-test")

    assert client.search_context("hello", count=50)["ok"] is True
    assert payloads == [{"query": "hello", "limit": 20}]


def test_find_channel_id_by_name_matches_case_insensitively_with_or_without_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return slack_response(
            json={
                "ok": True,
                "channels": [
                    {"id": "C1", "name": "general"},
                    {"id": "C2", "name": "random"},
                ],
            }
        )

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fake_request)
    client = SlackClient(lambda: "xoxp-test")

    assert client.find_channel_id_by_name("General") == "C1"
    assert client.find_channel_id_by_name("#random") == "C2"


def test_find_channel_id_by_name_paginates_until_match_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        {
            "ok": True,
            "channels": [{"id": "C1", "name": "general"}],
            "response_metadata": {"next_cursor": "page2"},
        },
        {"ok": True, "channels": [{"id": "C2", "name": "target"}]},
    ]
    calls: list[dict[str, object]] = []

    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        calls.append(dict(kwargs["params"]))
        return slack_response(json=pages[len(calls) - 1])

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fake_request)
    client = SlackClient(lambda: "xoxp-test")

    assert client.find_channel_id_by_name("target") == "C2"
    assert len(calls) == 2
    assert calls[1]["cursor"] == "page2"


def test_find_channel_id_by_name_returns_none_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return slack_response(json={"ok": True, "channels": []})

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fake_request)
    client = SlackClient(lambda: "xoxp-test")

    assert client.find_channel_id_by_name("missing") is None


def test_get_access_token_raises_auth_required_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_credentials = (
        Path(__file__).parent / ".scratch" / "missing-credentials.json"
    )
    if missing_credentials.exists():
        missing_credentials.unlink()
    monkeypatch.setattr(auth, "CREDENTIALS_PATH", missing_credentials)

    with pytest.raises(AuthRequiredError, match="認可コマンド"):
        auth.get_access_token()


def test_generate_self_signed_cert_files_produces_loadable_cert_and_key() -> None:
    cert_dir = Path(__file__).parent / ".scratch" / "certs"
    generated = auth._generate_self_signed_cert_files(cert_dir)
    try:
        cert = x509.load_pem_x509_certificate(generated.cert_path.read_bytes())
        key = serialization.load_pem_private_key(
            generated.key_path.read_bytes(), password=None
        )

        assert cert.subject.rfc4514_string() == "CN=localhost"
        assert key is not None
        assert generated.ssl_context is not None
    finally:
        auth._cleanup_generated_certificate(generated)
        if cert_dir.exists() and not any(cert_dir.iterdir()):
            cert_dir.rmdir()
        scratch_dir = cert_dir.parent
        if scratch_dir.exists() and not any(scratch_dir.iterdir()):
            scratch_dir.rmdir()


class _FakeClient:
    """Minimal stand-in for SlackClient used to unit test the MCP tools."""

    def __init__(
        self,
        *,
        search_response: dict[str, object] | None = None,
        replies_pages: list[dict[str, object]] | None = None,
    ) -> None:
        self.search_response = search_response or {}
        self.replies_pages = list(replies_pages or [])
        self.replies_calls: list[dict[str, object]] = []

    def resolve_user(self, user_id: str) -> str:
        return f"user:{user_id}"

    def resolve_mentions(self, text: str) -> str:
        return text

    def search_context(
        self, query: str, count: int = 10, cursor: str | None = None
    ) -> dict[str, object]:
        return self.search_response

    def conversations_replies(
        self, channel: str, ts: str, cursor: str | None = None, limit: int = 200
    ) -> dict[str, object]:
        self.replies_calls.append({"channel": channel, "ts": ts, "cursor": cursor})
        if not self.replies_pages:
            return {"messages": []}
        return self.replies_pages[
            min(len(self.replies_calls) - 1, len(self.replies_pages) - 1)
        ]

    def find_channel_id_by_name(
        self, name: str, *, include_archived: bool = False
    ) -> str | None:
        return {"general": "C1"}.get(name)


def test_slack_search_context_returns_tool_result_with_structured_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        search_response={
            "results": {
                "messages": [
                    {
                        "ts": "1700000000.000100",
                        "user": "U1",
                        "text": "hello",
                        "channel": "C1",
                        "permalink": "https://example.slack.com/p1",
                    }
                ]
            },
            "response_metadata": {"next_cursor": "next-page"},
        }
    )
    monkeypatch.setattr(server, "_get_client", lambda: fake_client)

    result = server.slack_search_context(query="hello")

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["next_cursor"] == "next-page"
    assert result.structured_content["results"][0]["channel_id"] == "C1"
    assert result.structured_content["results"][0]["author"] == "user:U1"
    assert "permalink: https://example.slack.com/p1" in result.content[0].text
    assert "次ページ取得用cursor: next-page" in result.content[0].text


def test_slack_search_context_returns_is_error_tool_result_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_auth_required() -> _FakeClient:
        raise AuthRequiredError("認可コマンドを実行してください")

    monkeypatch.setattr(server, "_get_client", raise_auth_required)

    result = server.slack_search_context(query="hello")

    assert result.is_error is True
    assert "認可コマンド" in result.content[0].text


def test_slack_get_thread_from_url_invalid_url_returns_is_error_without_crashing() -> (
    None
):
    # Regression test: with output_schema set, returning a bare str (instead of
    # ToolResult) used to crash with "structured_content must be a dict".
    result = server.slack_get_thread_from_url("not a url")

    assert result.is_error is True
    assert "Slack URL" in result.content[0].text


def test_slack_get_thread_from_url_builds_structured_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        replies_pages=[
            {
                "messages": [
                    {"ts": "1700000000.000100", "user": "U1", "text": "parent"},
                    {"ts": "1700000001.000200", "user": "U2", "text": "reply"},
                ]
            }
        ]
    )
    monkeypatch.setattr(server, "_get_client", lambda: fake_client)

    result = server.slack_get_thread_from_url(
        "https://example.slack.com/archives/C123ABC/p1700000000000100"
    )

    assert result.is_error is False
    assert result.structured_content["channel_id"] == "C123ABC"
    assert result.structured_content["reply_count"] == 1
    assert len(result.structured_content["messages"]) == 2
    assert "メッセージ数: 2（返信 1 件）" in result.content[0].text


def test_slack_get_thread_from_url_no_messages_returns_empty_structured_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: the previous implementation returned a bare str for the
    # empty case, which also crashed once output_schema was declared.
    fake_client = _FakeClient(replies_pages=[{"messages": []}])
    monkeypatch.setattr(server, "_get_client", lambda: fake_client)

    result = server.slack_get_thread_from_url(
        "https://example.slack.com/archives/C123ABC/p1700000000000100"
    )

    assert result.is_error is False
    assert result.structured_content["messages"] == []
    assert result.structured_content["reply_count"] == 0


def test_slack_get_thread_from_url_deduplicates_messages_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        replies_pages=[
            {
                "messages": [
                    {"ts": "1700000000.000100", "user": "U1", "text": "parent"},
                    {"ts": "1700000001.000200", "user": "U2", "text": "reply1"},
                ],
                "response_metadata": {"next_cursor": "page2"},
            },
            {
                "messages": [
                    # 親メッセージが2ページ目にも重複して現れるケース。
                    {"ts": "1700000000.000100", "user": "U1", "text": "parent"},
                    {"ts": "1700000002.000300", "user": "U3", "text": "reply2"},
                ]
            },
        ]
    )
    monkeypatch.setattr(server, "_get_client", lambda: fake_client)

    result = server.slack_get_thread_from_url(
        "https://example.slack.com/archives/C123ABC/p1700000000000100"
    )

    assert result.is_error is False
    assert len(result.structured_content["messages"]) == 3
    assert result.structured_content["reply_count"] == 2


def test_slack_get_thread_from_url_stops_at_max_pagination_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient()

    def never_ending_replies(
        channel: str, ts: str, cursor: str | None = None, limit: int = 200
    ) -> dict[str, object]:
        fake_client.replies_calls.append(
            {"channel": channel, "ts": ts, "cursor": cursor}
        )
        # Always returns a next_cursor so the loop would run forever without a cap.
        return {"messages": [], "response_metadata": {"next_cursor": "still-more"}}

    fake_client.conversations_replies = never_ending_replies  # type: ignore[method-assign]
    monkeypatch.setattr(server, "_get_client", lambda: fake_client)

    result = server.slack_get_thread_from_url(
        "https://example.slack.com/archives/C123ABC/p1700000000000100"
    )

    assert result.is_error is False
    assert len(fake_client.replies_calls) == server._MAX_THREAD_PAGES


def test_slack_resolve_channel_id_found_returns_structured_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient()
    monkeypatch.setattr(server, "_get_client", lambda: fake_client)

    result = server.slack_resolve_channel_id(channel_name="#general")

    assert result.is_error is False
    assert result.structured_content["channel_name"] == "general"
    assert result.structured_content["channel_id"] == "C1"
    assert result.structured_content["found"] is True
    assert "C1" in result.content[0].text


def test_slack_resolve_channel_id_not_found_returns_found_false_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient()
    monkeypatch.setattr(server, "_get_client", lambda: fake_client)

    result = server.slack_resolve_channel_id(channel_name="missing-channel")

    assert result.is_error is False
    assert result.structured_content["channel_id"] is None
    assert result.structured_content["found"] is False
    assert "見つかりませんでした" in result.content[0].text


def test_slack_resolve_channel_id_returns_is_error_tool_result_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_auth_required() -> _FakeClient:
        raise AuthRequiredError("認可コマンドを実行してください")

    monkeypatch.setattr(server, "_get_client", raise_auth_required)

    result = server.slack_resolve_channel_id(channel_name="general")

    assert result.is_error is True
    assert "認可コマンド" in result.content[0].text
