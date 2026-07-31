from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import ssl
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS_DIR = PROJECT_ROOT / ".credentials"
CREDENTIALS_PATH = CREDENTIALS_DIR / "credentials.json"
DEFAULT_REDIRECT_URI = "https://localhost:8000/callback"
OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
TOKEN_REFRESH_MARGIN_SECONDS = 120

# 書き込みスコープ(chat:write等)の追加は禁止。User token only, no bot token.
USER_SCOPES: tuple[str, ...] = (
    "search:read.public",
    "search:read.private",
    "search:read.im",
    "search:read.mpim",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
    "channels:read",
    "groups:read",
    "users:read",
)


class AuthRequiredError(RuntimeError):
    """Raised when Slack user-token authorization is required again."""


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True)
class GeneratedCertificate:
    cert_path: Path
    key_path: Path
    ssl_context: ssl.SSLContext


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = _load_oauth_config()
        redirect = _parse_redirect_uri(config.redirect_uri)
        state = secrets.token_urlsafe(32)
        authorize_url = _build_authorize_url(config, state)

        logger.info(
            "Slack認可URLをブラウザで開きます。開かない場合は以下をコピーしてください:"
        )
        logger.info("%s", authorize_url)
        webbrowser.open(authorize_url)

        generated_cert = _generate_self_signed_cert_files()
        try:
            code = _receive_oauth_code(redirect, state, generated_cert.ssl_context)
        finally:
            _cleanup_generated_certificate(generated_cert)

        token_response = _exchange_authorization_code(config, code)
        credentials = _credentials_from_token_response(token_response)
        _save_credentials(credentials)
        logger.info("Slackユーザートークンを保存しました: %s", CREDENTIALS_PATH)
    except SystemExit:
        raise
    except Exception as exc:
        logger.error("認可に失敗しました: %s", exc)
        raise SystemExit(1) from exc


def get_access_token() -> str:
    credentials = _load_credentials()
    access_token = credentials.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthRequiredError(
            "トークンが見つかりません。先に認可コマンドを実行してください: uv run slack-mcp-auth"
        )

    expires_at = credentials.get("expires_at")
    if expires_at is None or float(expires_at) > time.time():
        return access_token

    refresh_token = credentials.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthRequiredError(
            "Slackトークンの有効期限が切れました。再認可してください: uv run slack-mcp-auth"
        )

    try:
        config = _load_oauth_config()
        refreshed = _refresh_access_token(config, refresh_token)
        updated = _merge_refreshed_credentials(credentials, refreshed)
        _save_credentials(updated)
        fresh_access_token = updated.get("access_token")
        if not isinstance(fresh_access_token, str) or not fresh_access_token:
            raise ValueError("refresh response did not include access_token")
        return fresh_access_token
    except Exception as exc:
        raise AuthRequiredError(
            "Slackトークンの更新に失敗しました。再認可してください: uv run slack-mcp-auth"
        ) from exc


def _load_oauth_config() -> OAuthConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    client_id = os.getenv("SLACK_CLIENT_ID")
    client_secret = os.getenv("SLACK_CLIENT_SECRET")
    redirect_uri = os.getenv("SLACK_REDIRECT_URI") or DEFAULT_REDIRECT_URI

    missing = [
        name
        for name, value in (
            ("SLACK_CLIENT_ID", client_id),
            ("SLACK_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + f". Set them in {PROJECT_ROOT / '.env'}."
        )

    return OAuthConfig(
        client_id=str(client_id),
        client_secret=str(client_secret),
        redirect_uri=redirect_uri,
    )


def _parse_redirect_uri(redirect_uri: str) -> tuple[int, str]:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "https":
        raise ValueError("SLACK_REDIRECT_URI must use https://")
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("SLACK_REDIRECT_URI must point to localhost")

    return parsed.port or 443, parsed.path or "/callback"


def _build_authorize_url(config: OAuthConfig, state: str) -> str:
    query = urlencode(
        {
            "client_id": config.client_id,
            "user_scope": ",".join(USER_SCOPES),
            "redirect_uri": config.redirect_uri,
            "state": state,
        }
    )
    return f"https://slack.com/oauth/v2/authorize?{query}"


def _generate_self_signed_cert_files(
    cert_dir: Path | None = None,
) -> GeneratedCertificate:
    target_dir = cert_dir or CREDENTIALS_DIR
    _ensure_secure_credentials_dir(target_dir)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    suffix = secrets.token_hex(8)
    cert_path = target_dir / f"oauth-localhost-{suffix}.crt"
    key_path = target_dir / f"oauth-localhost-{suffix}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    _chmod_posix(cert_path, 0o600)
    _chmod_posix(key_path, 0o600)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return GeneratedCertificate(
        cert_path=cert_path, key_path=key_path, ssl_context=context
    )


def _cleanup_generated_certificate(generated: GeneratedCertificate) -> None:
    for path in (generated.cert_path, generated.key_path):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("一時証明書ファイルの削除に失敗しました (%s): %s", path, exc)


def _receive_oauth_code(
    redirect: tuple[int, str],
    expected_state: str,
    ssl_context: ssl.SSLContext,
) -> str:
    port, expected_path = redirect
    callback: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            if parsed.path != expected_path:
                self._send_html(404, "Not Found", "認可URLのパスが正しくありません。")
                return

            received_state = params.get("state", [""])[0]
            if not secrets.compare_digest(received_state, expected_state):
                callback["error"] = (
                    "stateパラメータが一致しません。認可をやり直してください。"
                )
                self._send_html(400, "Invalid state", callback["error"])
                return

            slack_error = params.get("error", [""])[0]
            if slack_error:
                callback["error"] = f"Slack認可エラー: {slack_error}"
                self._send_html(400, "Authorization error", callback["error"])
                return

            code = params.get("code", [""])[0]
            if not code:
                callback["error"] = "認可コードが見つかりません。"
                self._send_html(400, "Missing code", callback["error"])
                return

            callback["code"] = code
            self._send_html(200, "認可完了", "認可完了。ブラウザを閉じてください。")

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("OAuth callback: " + format, *args)

        def _send_html(self, status: int, title: str, message: str) -> None:
            body = (
                "<!doctype html><html lang='ja'><meta charset='utf-8'>"
                f"<title>{title}</title><body><h1>{title}</h1><p>{message}</p></body></html>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("localhost", port), CallbackHandler)
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    logger.info("HTTPSコールバック待受中: https://localhost:%s%s", port, expected_path)
    try:
        server.handle_request()
    finally:
        server.server_close()

    if "error" in callback:
        raise RuntimeError(callback["error"])
    if "code" not in callback:
        raise RuntimeError("OAuth callback did not include an authorization code")
    return callback["code"]


def _exchange_authorization_code(config: OAuthConfig, code: str) -> dict[str, Any]:
    response = _post_oauth_access(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "redirect_uri": config.redirect_uri,
        }
    )
    if not _extract_access_token(response):
        raise RuntimeError("Slack response did not include authed_user.access_token")
    return response


def _refresh_access_token(config: OAuthConfig, refresh_token: str) -> dict[str, Any]:
    response = _post_oauth_access(
        {
            "grant_type": "refresh_token",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "refresh_token": refresh_token,
        }
    )
    access_token = _extract_access_token(response)
    if not access_token or not access_token.startswith("xoxp-"):
        raise RuntimeError("Slack refresh response did not include access_token")
    return response


def _post_oauth_access(data: dict[str, str]) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        response = client.post(OAUTH_ACCESS_URL, data=data)
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Slack returned a non-object response")
    if not payload.get("ok", False):
        error = payload.get("error", "unknown_error")
        needed = payload.get("needed")
        detail = f"Slack API error: {error}"
        if needed:
            detail += f" (needed scope: {needed})"
        raise RuntimeError(detail)
    return payload


def _credentials_from_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    authed_user = payload.get("authed_user")
    if not isinstance(authed_user, dict):
        raise RuntimeError("Slack response did not include authed_user")

    access_token = authed_user.get("access_token")
    if not isinstance(access_token, str) or not access_token.startswith("xoxp-"):
        raise RuntimeError("Slack response did not include a user token (xoxp-)")

    refresh_token = authed_user.get("refresh_token") or payload.get("refresh_token")
    expires_in = authed_user.get("expires_in") or payload.get("expires_in")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token if isinstance(refresh_token, str) else None,
        "expires_at": _compute_expires_at(expires_in),
        "token_type": authed_user.get("token_type") or payload.get("token_type"),
        "scope": authed_user.get("scope") or payload.get("scope"),
        "authed_user": {"id": authed_user.get("id")},
        "saved_at": int(time.time()),
    }


def _merge_refreshed_credentials(
    current: dict[str, Any],
    refreshed: dict[str, Any],
) -> dict[str, Any]:
    access_token = _extract_access_token(refreshed)
    refresh_token = _extract_refresh_token(refreshed) or current.get("refresh_token")
    expires_in = _extract_expires_in(refreshed)
    authed_user = (
        current.get("authed_user")
        if isinstance(current.get("authed_user"), dict)
        else {}
    )

    updated = dict(current)
    updated.update(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": _compute_expires_at(expires_in),
            "token_type": refreshed.get("token_type") or updated.get("token_type"),
            "scope": refreshed.get("scope") or updated.get("scope"),
            "authed_user": authed_user,
            "saved_at": int(time.time()),
        }
    )
    return updated


def _extract_access_token(payload: dict[str, Any]) -> str | None:
    authed_user = payload.get("authed_user")
    if isinstance(authed_user, dict) and isinstance(
        authed_user.get("access_token"), str
    ):
        return authed_user["access_token"]
    token = payload.get("access_token")
    return token if isinstance(token, str) else None


def _extract_refresh_token(payload: dict[str, Any]) -> str | None:
    authed_user = payload.get("authed_user")
    if isinstance(authed_user, dict) and isinstance(
        authed_user.get("refresh_token"), str
    ):
        return authed_user["refresh_token"]
    token = payload.get("refresh_token")
    return token if isinstance(token, str) else None


def _extract_expires_in(payload: dict[str, Any]) -> Any:
    authed_user = payload.get("authed_user")
    if isinstance(authed_user, dict) and authed_user.get("expires_in") is not None:
        return authed_user.get("expires_in")
    return payload.get("expires_in")


def _compute_expires_at(expires_in: Any) -> int | None:
    if expires_in is None:
        return None
    seconds = int(expires_in)
    return int(time.time()) + max(0, seconds - TOKEN_REFRESH_MARGIN_SECONDS)


def _load_credentials() -> dict[str, Any]:
    if not CREDENTIALS_PATH.exists():
        raise AuthRequiredError(
            "トークンが見つかりません。先に認可コマンドを実行してください: uv run slack-mcp-auth"
        )
    try:
        with CREDENTIALS_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthRequiredError(
            "トークンを読み込めません。再認可してください: uv run slack-mcp-auth"
        ) from exc
    if not isinstance(payload, dict):
        raise AuthRequiredError(
            "トークン形式が不正です。再認可してください: uv run slack-mcp-auth"
        )
    return payload


def _save_credentials(credentials: dict[str, Any]) -> None:
    _ensure_secure_credentials_dir(CREDENTIALS_DIR)
    temp_path = CREDENTIALS_PATH.with_name(
        f"{CREDENTIALS_PATH.name}.{secrets.token_hex(8)}"
    )
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(credentials, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    _chmod_posix(temp_path, 0o600)
    temp_path.replace(CREDENTIALS_PATH)
    _chmod_posix(CREDENTIALS_PATH, 0o600)


def _ensure_secure_credentials_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod_posix(path, 0o700)


def _chmod_posix(path: Path, mode: int) -> None:
    if os.name != "nt":
        os.chmod(path, mode)


__all__ = ["AuthRequiredError", "get_access_token", "main"]
