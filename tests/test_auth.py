"""Token cache and refresh behaviour. No network involved."""

import json
import os
import pathlib
import stat
import tempfile
import time

import pytest

from ipaapi.auth import Credentials, TokenCache, refresh
from ipaapi.auth import _credentials_from_token
from ipaapi.errors import AuthenticationError

CLIENT = "test-client"


@pytest.fixture
def cache():
    return TokenCache(path=str(pathlib.Path(tempfile.mkdtemp()) / "token.json"))


def creds(**kw):
    kw.setdefault("access_token", "tok")
    kw.setdefault("application_name", "PythonAPI")
    kw.setdefault("host", "analysis.ingenuity.com")
    return Credentials(**kw)


# -- cache -----------------------------------------------------------------


def test_round_trip(cache):
    cache.put(CLIENT, creds(expires_at=time.time() + 3600))
    got = cache.get(CLIENT, "PythonAPI", "analysis.ingenuity.com")
    assert got is not None and got.access_token == "tok"


def test_cache_file_is_owner_only(cache):
    cache.put(CLIENT, creds())
    mode = stat.S_IMODE(os.stat(cache.path).st_mode)
    assert mode == 0o600


def test_expired_entries_are_withheld_by_default(cache):
    cache.put(CLIENT, creds(expires_at=time.time() - 10, refresh_token="r"))
    assert cache.get(CLIENT, "PythonAPI", "analysis.ingenuity.com") is None


def test_expired_entries_are_available_for_refresh(cache):
    """The refresh token in an expired entry is exactly what avoids a browser."""
    cache.put(CLIENT, creds(expires_at=time.time() - 10, refresh_token="r"))
    stale = cache.get(
        CLIENT, "PythonAPI", "analysis.ingenuity.com", allow_expired=True
    )
    assert stale is not None
    assert stale.refresh_token == "r"


def test_entries_are_keyed_by_client_application_and_host(cache):
    cache.put(CLIENT, creds())
    assert cache.get("other-client", "PythonAPI", "analysis.ingenuity.com") is None
    assert cache.get(CLIENT, "OtherApp", "analysis.ingenuity.com") is None
    assert cache.get(CLIENT, "PythonAPI", "other.host") is None


def test_corrupt_cache_is_a_miss_not_a_crash(cache):
    pathlib.Path(cache.path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(cache.path).write_text("{ not json")
    assert cache.get(CLIENT, "PythonAPI", "analysis.ingenuity.com") is None


def test_expiry_margin_is_sixty_seconds():
    assert creds(expires_at=time.time() + 3600).is_expired is False
    assert creds(expires_at=time.time() + 30).is_expired is True
    assert creds().is_expired is False


# -- building credentials from a token response ----------------------------


def test_expires_in_is_converted_to_an_absolute_time():
    before = time.time()
    c = _credentials_from_token(
        {"access_token": "a", "expires_in": 600}, "h", "app"
    )
    assert before + 590 <= c.expires_at <= time.time() + 600


def test_refresh_response_without_a_new_refresh_token_keeps_the_old_one():
    """Servers commonly omit it, meaning 'keep using the one you have'."""
    previous = creds(refresh_token="original")
    renewed = _credentials_from_token(
        {"access_token": "new", "expires_in": 600}, "h", "app", previous=previous
    )
    assert renewed.refresh_token == "original"
    assert renewed.access_token == "new"


def test_a_new_refresh_token_replaces_the_old_one():
    previous = creds(refresh_token="original")
    renewed = _credentials_from_token(
        {"access_token": "new", "refresh_token": "rotated"}, "h", "app", previous=previous
    )
    assert renewed.refresh_token == "rotated"


def test_token_response_without_access_token_is_rejected():
    with pytest.raises(AuthenticationError, match="no access_token"):
        _credentials_from_token({"expires_in": 60}, "h", "app")


# -- refresh ---------------------------------------------------------------


def test_refresh_without_a_refresh_token_explains_itself():
    with pytest.raises(AuthenticationError, match="cannot be renewed"):
        refresh(creds(expires_at=time.time() - 10))


# -- relocating the cache --------------------------------------------------


def test_token_file_env_var_overrides_the_default(monkeypatch=None):
    from ipaapi.auth import TOKEN_FILE_ENV, _default_cache_path

    old = os.environ.get(TOKEN_FILE_ENV)
    os.environ[TOKEN_FILE_ENV] = "/tmp/somewhere/token.json"
    try:
        assert _default_cache_path() == "/tmp/somewhere/token.json"
    finally:
        if old is None:
            del os.environ[TOKEN_FILE_ENV]
        else:
            os.environ[TOKEN_FILE_ENV] = old


def test_unwritable_cache_is_reported_not_swallowed(capsys=None):
    """A cache that silently never writes looks exactly like instant expiry."""
    import io
    from contextlib import redirect_stdout

    cache = TokenCache(path="/proc/definitely/not/writable/token.json")
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cache.put(CLIENT, creds())          # must not raise
    output = buffer.getvalue()
    assert "could not write the token cache" in output
    assert "IPAAPI_TOKEN_FILE" in output    # tells the user how to fix it
