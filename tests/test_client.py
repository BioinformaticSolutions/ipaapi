"""Client behaviour that can be checked without touching the network."""

import pytest

from ipaapi import AnalysisStatus, Credentials, IPAClient
from ipaapi.errors import SubmissionError


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def test_status_codes_map_to_states():
    assert AnalysisStatus.from_code("3") is AnalysisStatus.SUCCEEDED
    assert AnalysisStatus.from_code("4") is AnalysisStatus.FAILED
    assert AnalysisStatus.from_code("5") is AnalysisStatus.CANCELED
    assert AnalysisStatus.from_code("1") is AnalysisStatus.IN_PROGRESS
    assert AnalysisStatus.from_code("") is AnalysisStatus.IN_PROGRESS
    assert AnalysisStatus.SUCCEEDED.is_terminal
    assert not AnalysisStatus.IN_PROGRESS.is_terminal
    assert AnalysisStatus.SUCCEEDED.succeeded
    assert not AnalysisStatus.FAILED.succeeded


def test_parse_analysis_ids_happy_path():
    ids = IPAClient._parse_analysis_ids(FakeResponse("abc-1,abc-2"), expected=2)
    assert ids == ["abc-1", "abc-2"]


def test_parse_analysis_ids_rejects_http_error():
    with pytest.raises(SubmissionError, match="HTTP 401"):
        IPAClient._parse_analysis_ids(FakeResponse("nope", status_code=401), expected=1)


def test_parse_analysis_ids_rejects_html_error_page():
    body = "<html><body>Internal error</body></html>"
    with pytest.raises(SubmissionError, match="does not look like analysis IDs"):
        IPAClient._parse_analysis_ids(FakeResponse(body), expected=1)


def test_credentials_never_leak_the_token_in_repr():
    creds = Credentials(access_token="supersecrettoken")
    assert "supersecrettoken" not in repr(creds)
    assert repr(creds).endswith("access_token=***oken)")


def test_expiry_uses_a_safety_margin():
    import time

    assert Credentials("t", expires_at=time.time() + 3600).is_expired is False
    assert Credentials("t", expires_at=time.time() + 10).is_expired is True
    assert Credentials("t").is_expired is False  # unknown expiry
