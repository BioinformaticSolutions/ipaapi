"""Browser-based OAuth 2.0 login against QIAGEN's authorization server.

The flow is authorization code with PKCE. A short-lived HTTP server is bound to
the loopback interface to catch the redirect, the user authorizes in their
browser, and the resulting code is exchanged for an access token.

This is the same flow the original demo used, with the sharp edges removed:

* the callback is awaited on a :class:`threading.Event` rather than a spin loop
  that pegged a CPU core;
* the ``state`` parameter is verified, closing the CSRF hole left by discarding
  it;
* the login times out instead of hanging forever if the user never authorizes;
* the callback server is always shut down, so a second login in the same process
  does not fail on an already-bound port;
* an ``error`` response from the authorization server is surfaced as an
  exception rather than an infinite wait;
* tokens can be cached to disk so repeat runs skip the browser entirely.

.. note::
   The redirect URI must exactly match what is registered for the OAuth client.
   For the public client ID shipped as :data:`DEFAULT_CLIENT_ID` that is
   ``http://localhost:8000``, so the callback port defaults to 8000 and changing
   it will generally cause the authorization server to reject the request.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import secrets
import stat
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .errors import AuthenticationError

__all__ = [
    "Credentials",
    "TokenCache",
    "login",
    "refresh",
    "DEFAULT_CLIENT_ID",
    "AUTHORIZATION_BASE_URL",
    "TOKEN_URL",
    "DEFAULT_HOST",
]

#: Public client ID usable by any IPA user; not a secret.
DEFAULT_CLIENT_ID = "1571511054-1646124475-167497993-BQfZBb"
AUTHORIZATION_BASE_URL = "https://apps.ingenuity.com/qiaoauth/oauth/authorize"
TOKEN_URL = "https://apps.ingenuity.com/qiaoauth/oauth/token"
DEFAULT_HOST = "analysis.ingenuity.com"
DEFAULT_REDIRECT_URI = "http://localhost:8000"
DEFAULT_APPLICATION_NAME = "PythonAPI"

_SUCCESS_PAGE = b"""<!doctype html><html><head><title>IPA login</title></head>
<body style="font-family:system-ui;margin:3rem">
<h2>Authorization received</h2>
<p>You can close this window and return to Python.</p>
</body></html>"""

_FAILURE_PAGE = b"""<!doctype html><html><head><title>IPA login</title></head>
<body style="font-family:system-ui;margin:3rem">
<h2>Authorization failed</h2>
<p>The authorization server reported an error. Check the Python console.</p>
</body></html>"""


@dataclass
class Credentials:
    """An access token and the context needed to use it.

    Attributes:
        access_token: Bearer token for the IPA API.
        host: API host the token is valid against.
        application_name: ``applicationname`` sent with every request; IPA uses
            it to scope datasets and analyses.
        expires_at: Unix timestamp of expiry, when the server reported one.
        refresh_token: Refresh token, when the server issued one.
        cookie_file: Cookie file reported by the token response, if any.
    """

    access_token: str
    host: str = DEFAULT_HOST
    application_name: str = DEFAULT_APPLICATION_NAME
    expires_at: Optional[float] = None
    refresh_token: Optional[str] = None
    cookie_file: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.access_token:
            raise AuthenticationError("Credentials require a non-empty access token.")

    @property
    def is_expired(self) -> bool:
        """Whether the token is known to have expired (60s safety margin)."""
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - 60)

    @property
    def auth_header(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Credentials":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_token(
        cls,
        access_token: str,
        host: str = DEFAULT_HOST,
        application_name: str = DEFAULT_APPLICATION_NAME,
    ) -> "Credentials":
        """Wrap a token obtained elsewhere, e.g. from an environment variable."""
        return cls(
            access_token=access_token,
            host=host,
            application_name=application_name,
        )

    def __repr__(self) -> str:  # never print the token
        tail = self.access_token[-4:] if len(self.access_token) > 4 else "?"
        return (
            f"Credentials(host={self.host!r}, application_name="
            f"{self.application_name!r}, access_token=***{tail})"
        )


def _default_cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "ipaapi", "token.json")


@dataclass
class TokenCache:
    """Stores tokens on disk between runs, keyed by client and application.

    The file is written with owner-only permissions. Cached tokens are ignored
    once expired, and a corrupt cache is treated as a miss rather than an error.
    """

    path: str = field(default_factory=_default_cache_path)

    def _load_all(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _key(client_id: str, application_name: str, host: str) -> str:
        return f"{client_id}|{application_name}|{host}"

    def get(
        self,
        client_id: str,
        application_name: str,
        host: str,
        allow_expired: bool = False,
    ) -> Optional[Credentials]:
        """Return cached credentials, or ``None`` on a miss.

        Expired entries are withheld unless *allow_expired* is set -- which
        :func:`login` does, because an expired entry still carries the refresh
        token needed to get a new one without a browser.
        """
        entry = self._load_all().get(self._key(client_id, application_name, host))
        if not isinstance(entry, dict):
            return None
        try:
            creds = Credentials.from_dict(entry)
        except (TypeError, AuthenticationError):
            return None
        if creds.is_expired and not allow_expired:
            return None
        return creds

    def put(self, client_id: str, credentials: Credentials) -> None:
        """Persist *credentials*.

        A cache failure is reported but not fatal -- losing the cache costs an
        extra login, not the run. It is not swallowed silently, because a cache
        that never writes looks exactly like a token that expires instantly.
        """
        data = self._load_all()
        data[self._key(client_id, credentials.application_name, credentials.host)] = (
            credentials.to_dict()
        )
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(
                f"Warning: could not write the token cache at {self.path!r} ({exc}). "
                "You will be asked to log in again next time."
            )

    def clear(self) -> None:
        """Remove the cache file if present."""
        try:
            os.remove(self.path)
        except OSError:
            pass


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that records the authorization response."""

    server_version = "ipaapi"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        params = parse_qs(urlparse(self.path).query)
        result = self.server.result  # type: ignore[attr-defined]

        if "code" in params or "error" in params:
            ok = "code" in params
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_PAGE if ok else _FAILURE_PAGE)
            result.update(
                code=params.get("code", [None])[0],
                state=params.get("state", [None])[0],
                error=params.get("error", [None])[0],
                error_description=params.get("error_description", [None])[0],
            )
            self.server.done.set()  # type: ignore[attr-defined]
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, fmt: str, *args) -> None:  # silence stderr access log
        return


class _CallbackServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, address):
        super().__init__(address, _CallbackHandler)
        self.result: Dict[str, Optional[str]] = {}
        self.done = threading.Event()


def _credentials_from_token(
    token: dict,
    host: str,
    application_name: str,
    previous: Optional[Credentials] = None,
) -> Credentials:
    """Build :class:`Credentials` from an OAuth token response."""
    access_token = token.get("access_token")
    if not access_token:
        raise AuthenticationError("Token response contained no access_token.")

    expires_at = token.get("expires_at")
    if expires_at is None and token.get("expires_in") is not None:
        try:
            expires_at = time.time() + float(token["expires_in"])
        except (TypeError, ValueError):
            expires_at = None

    # A refresh response often omits the refresh token, meaning "keep using the
    # one you have". Dropping it would force a browser login next time.
    refresh_token = token.get("refresh_token") or (
        previous.refresh_token if previous else None
    )

    return Credentials(
        access_token=access_token,
        host=host,
        application_name=application_name,
        expires_at=float(expires_at) if expires_at is not None else None,
        refresh_token=refresh_token,
        cookie_file=token.get("cookieFile") or (previous.cookie_file if previous else None),
    )


def refresh(
    credentials: Credentials,
    client_id: str = DEFAULT_CLIENT_ID,
    token_url: str = TOKEN_URL,
    cache: Optional[TokenCache] = None,
) -> Credentials:
    """Exchange a refresh token for a fresh access token. No browser involved.

    This is what makes unattended and headless use practical: a token copied
    from a machine that can run a browser keeps renewing itself on a server that
    cannot, for as long as the refresh token stays valid.

    Args:
        credentials: Existing credentials carrying a refresh token. May be
            expired -- that is the normal case here.
        client_id: OAuth client the refresh token belongs to.
        token_url: Token endpoint.
        cache: If given, the renewed credentials are written back to it.

    Raises:
        AuthenticationError: If there is no refresh token, or the server
            refuses to honour it (typically because it has itself expired or
            been revoked, in which case a browser login is required).
    """
    if not credentials.refresh_token:
        raise AuthenticationError(
            "The cached token has expired and carries no refresh token, so it "
            "cannot be renewed without logging in again."
        )

    try:
        from requests_oauthlib import OAuth2Session
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AuthenticationError(
            "requests-oauthlib is required to refresh a token."
        ) from exc

    oauth = OAuth2Session(client_id)
    try:
        token = oauth.refresh_token(
            token_url,
            refresh_token=credentials.refresh_token,
            client_id=client_id,
        )
    except Exception as exc:
        raise AuthenticationError(f"Refreshing the access token failed: {exc}") from exc

    renewed = _credentials_from_token(
        token,
        host=credentials.host,
        application_name=credentials.application_name,
        previous=credentials,
    )
    if cache is not None:
        cache.put(client_id, renewed)
    return renewed


def _start_callback_server(host: str, port: int) -> _CallbackServer:
    try:
        server = _CallbackServer((host, port))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            raise AuthenticationError(
                f"Cannot listen on {host}:{port} for the OAuth redirect ({exc.strerror}). "
                "Another process is probably using that port -- stop it, or pass a "
                "different redirect_uri. Note the redirect URI must be registered with "
                "the OAuth client, so for the default public client it has to be "
                f"{DEFAULT_REDIRECT_URI}."
            ) from exc
        raise AuthenticationError(f"Could not start the OAuth callback server: {exc}") from exc
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def login(
    client_id: str = DEFAULT_CLIENT_ID,
    application_name: str = DEFAULT_APPLICATION_NAME,
    host: str = DEFAULT_HOST,
    authorization_base_url: str = AUTHORIZATION_BASE_URL,
    token_url: str = TOKEN_URL,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scope: Optional[str] = None,
    timeout: float = 300.0,
    open_browser: bool = True,
    cache: Optional[TokenCache] = None,
    force: bool = False,
    **fetch_token_kwargs,
) -> Credentials:
    """Run the browser OAuth flow and return :class:`Credentials`.

    Args:
        client_id: OAuth client. The default is a public client any IPA user may
            use; it is not a secret.
        application_name: ``applicationname`` IPA should associate the session
            with. Datasets and analyses are scoped to it.
        host: IPA API host the resulting token will be used against.
        authorization_base_url: Authorization endpoint.
        token_url: Token exchange endpoint.
        redirect_uri: Loopback URI the authorization server redirects to. Must
            match the client registration.
        scope: Optional scope string.
        timeout: Seconds to wait for the user to finish authorizing.
        open_browser: Open the URL automatically. When ``False``, the URL is
            printed for the user to open themselves -- useful over SSH.
        cache: Optional :class:`TokenCache`. When given, a valid cached token is
            reused and new tokens are written back.
        force: Ignore any cached token and re-authorize.
        **fetch_token_kwargs: Extra keyword arguments passed to
            ``OAuth2Session.fetch_token``, e.g. ``include_client_id=True`` if the
            authorization server requires the client ID in the token request.

    Raises:
        AuthenticationError: On timeout, state mismatch, an error response from
            the authorization server, or a failed token exchange.
    """
    if cache is not None and not force:
        cached = cache.get(client_id, application_name, host)
        if cached is not None:
            return cached

        # Expired, but a refresh token renews it without touching a browser.
        stale = cache.get(client_id, application_name, host, allow_expired=True)
        if stale is not None and stale.refresh_token:
            try:
                return refresh(stale, client_id=client_id, token_url=token_url, cache=cache)
            except AuthenticationError as exc:
                print(f"Warning: {exc}\nFalling back to browser login.")

    try:
        from requests_oauthlib import OAuth2Session
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AuthenticationError(
            "requests-oauthlib is required for browser login. Install it with "
            "`pip install requests-oauthlib`."
        ) from exc

    parsed = urlparse(redirect_uri)
    bind_host = parsed.hostname or "localhost"
    bind_port = parsed.port or 80
    # Bind the loopback interface regardless of how the URI spells it.
    listen_host = "127.0.0.1" if bind_host in ("localhost", "127.0.0.1") else bind_host

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )

    oauth = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope)
    authorization_url, expected_state = oauth.authorization_url(
        authorization_base_url,
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )

    server = _start_callback_server(listen_host, bind_port)
    try:
        if open_browser:
            opened = webbrowser.open(authorization_url)
            if not opened:
                print("Could not open a browser automatically. Visit:\n" + authorization_url)
        else:
            print("Open this URL to authorize:\n" + authorization_url)

        if not server.done.wait(timeout=timeout):
            raise AuthenticationError(
                f"Timed out after {timeout:g}s waiting for authorization. "
                "Nothing arrived at the redirect URI -- was the browser window "
                "closed before granting access?"
            )

        result = server.result
        if result.get("error"):
            detail = result.get("error_description") or ""
            raise AuthenticationError(
                f"Authorization server returned an error: {result['error']}"
                + (f" ({detail})" if detail else "")
            )

        returned_state = result.get("state")
        if returned_state != expected_state:
            raise AuthenticationError(
                "OAuth state mismatch between the request and the redirect. The "
                "response may not correspond to this login attempt; refusing to "
                "exchange the code."
            )

        code = result.get("code")
        if not code:
            raise AuthenticationError("Redirect carried no authorization code.")

        try:
            token = oauth.fetch_token(
                token_url,
                code=code,
                code_verifier=code_verifier,
                state=returned_state,
                **fetch_token_kwargs,
            )
        except Exception as exc:
            raise AuthenticationError(f"Token exchange failed: {exc}") from exc
    finally:
        server.shutdown()
        server.server_close()

    credentials = _credentials_from_token(
        token, host=host, application_name=application_name
    )
    if cache is not None:
        cache.put(client_id, credentials)
    return credentials
