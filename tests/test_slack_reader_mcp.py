from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from slack_reader_mcp import auth, server
from slack_reader_mcp.auth import AuthRequiredError
from slack_reader_mcp.slack_client import EndpointNotAllowedError, SlackAPIError, SlackClient


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
        "https://example.slack.com/archives/C123ABC/p1600000000123456?thread_ts=1599999999.000200&cid=C123ABC"
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
    expected = datetime.fromtimestamp(1600000000.123456, tz=timezone.utc).astimezone().isoformat(timespec="seconds")

    assert server.slack_ts_to_local_iso("1600000000.123456") == expected


def test_endpoint_whitelist_blocks_non_readonly_method_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_request(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("HTTP request should not be sent for a disallowed endpoint")

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fail_request)
    client = SlackClient(lambda: "xoxp-test")

    with pytest.raises(EndpointNotAllowedError):
        client._request("POST", "chat.postMessage", json={"channel": "C1", "text": "blocked"})


def test_resolve_mentions_replaces_plain_and_labeled_mentions() -> None:
    client = SlackClient(lambda: "xoxp-test")
    client.users_info = lambda user: {  # type: ignore[method-assign]
        "ok": True,
        "user": {"profile": {"display_name": f"name-{user}"}},
    }

    assert client.resolve_mentions("Hi <@U123> and <@U456|fallback>") == "Hi @name-U123 and @name-U456"


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


def test_rate_limit_http_status_raises_guidance_without_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return slack_response(429, headers={"Retry-After": "10"})

    monkeypatch.setattr("slack_reader_mcp.slack_client.httpx.request", fake_request)
    monkeypatch.setattr("slack_reader_mcp.slack_client.time.sleep", lambda seconds: None)
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


def test_get_access_token_raises_auth_required_when_credentials_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    missing_credentials = Path(__file__).parent / ".scratch" / "missing-credentials.json"
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
        key = serialization.load_pem_private_key(generated.key_path.read_bytes(), password=None)

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
