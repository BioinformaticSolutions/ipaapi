"""Filing files into submitted/ and failed/, and telling quota apart from failure."""

import pathlib
import tempfile

import pytest

from ipaapi.client import QUOTA_PATTERNS, IPAClient, looks_like_quota
from ipaapi.cli import build_parser, discover_files
from ipaapi.errors import QuotaExceededError, SubmissionError
from ipaapi.triage import FAILED_DIRNAME, SUBMITTED_DIRNAME, Triage


@pytest.fixture
def workdir():
    tmp = pathlib.Path(tempfile.mkdtemp())
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp / name).write_text("id\tfc\nENSG1\t2.0\n")
    return tmp


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


# -- quota detection -------------------------------------------------------


def test_http_429_is_a_quota_response():
    assert looks_like_quota(429, "")


def test_quota_wording_is_matched_case_insensitively():
    assert looks_like_quota(400, "Analysis QUOTA exceeded for this account")
    assert looks_like_quota(200, "your allowance has been used up")


def test_unrelated_failures_are_not_quota():
    assert not looks_like_quota(400, "Invalid geneidtype 'ensmebl'")
    assert not looks_like_quota(500, "Internal server error")


def test_quota_rejection_raises_the_specific_error():
    with pytest.raises(QuotaExceededError):
        IPAClient._parse_analysis_ids(
            FakeResponse("Monthly analysis quota exceeded", 403), expected=1
        )


def test_quota_error_is_still_a_submission_error():
    """So `except SubmissionError` keeps catching everything it used to."""
    assert issubclass(QuotaExceededError, SubmissionError)


def test_other_rejections_stay_generic():
    with pytest.raises(SubmissionError) as _:
        IPAClient._parse_analysis_ids(FakeResponse("Bad request", 400), expected=1)
    try:
        IPAClient._parse_analysis_ids(FakeResponse("Bad request", 400), expected=1)
    except SubmissionError as exc:
        assert not isinstance(exc, QuotaExceededError)


def test_the_raw_body_is_reported_so_a_wrong_guess_is_visible():
    try:
        IPAClient._parse_analysis_ids(FakeResponse("Some odd message", 400), expected=1)
    except SubmissionError as exc:
        assert "Some odd message" in str(exc)
        assert exc.body == "Some odd message"


def test_quota_patterns_are_exposed_for_tuning():
    assert "quota" in QUOTA_PATTERNS


# -- filing ----------------------------------------------------------------


def test_submitted_files_move(workdir):
    t = Triage(workdir)
    t.mark_submitted(workdir / "a.txt")
    assert (workdir / SUBMITTED_DIRNAME / "a.txt").exists()
    assert not (workdir / "a.txt").exists()


def test_failed_files_move_with_a_note(workdir):
    t = Triage(workdir)
    t.mark_failed(workdir / "b.txt", "column 9 does not exist")
    moved = workdir / FAILED_DIRNAME / "b.txt"
    assert moved.exists()
    note = workdir / FAILED_DIRNAME / "b.txt.error.txt"
    assert note.exists()
    assert "column 9 does not exist" in note.read_text()


def test_left_files_are_untouched(workdir):
    t = Triage(workdir)
    t.mark_left(workdir / "c.txt")
    assert (workdir / "c.txt").exists()
    assert t.left == [workdir / "c.txt"]


def test_directories_are_only_created_when_used(workdir):
    t = Triage(workdir)
    t.mark_left(workdir / "c.txt")
    assert not (workdir / SUBMITTED_DIRNAME).exists()
    assert not (workdir / FAILED_DIRNAME).exists()


def test_dry_run_moves_nothing(workdir):
    t = Triage(workdir, dry_run=True)
    t.mark_submitted(workdir / "a.txt")
    t.mark_failed(workdir / "b.txt", "reason")
    assert (workdir / "a.txt").exists()
    assert (workdir / "b.txt").exists()
    assert not (workdir / SUBMITTED_DIRNAME).exists()
    assert "would be moved" in t.summary()


def test_name_collisions_do_not_overwrite(workdir):
    t = Triage(workdir)
    t.mark_submitted(workdir / "a.txt")
    (workdir / "a.txt").write_text("a second file with the same name\n")
    t.mark_submitted(workdir / "a.txt")
    filed = sorted(p.name for p in (workdir / SUBMITTED_DIRNAME).iterdir())
    assert filed == ["a-1.txt", "a.txt"]


def test_summary_counts_each_category(workdir):
    t = Triage(workdir)
    t.mark_submitted(workdir / "a.txt")
    t.mark_failed(workdir / "b.txt", "nope")
    t.mark_left(workdir / "c.txt")
    summary = t.summary()
    assert "1 file(s) moved to submitted/" in summary
    assert "1 file(s) moved to failed/" in summary
    assert "1 file(s) left in place" in summary


# -- discovery must not re-ingest its own output ---------------------------


def test_already_filed_files_are_not_picked_up_again(workdir):
    t = Triage(workdir)
    t.mark_submitted(workdir / "a.txt")
    t.mark_failed(workdir / "b.txt", "nope")
    found = [p.name for p in discover_files(str(workdir), recursive=True)]
    assert found == ["c.txt"]


def test_non_recursive_search_is_unaffected(workdir):
    Triage(workdir).mark_submitted(workdir / "a.txt")
    found = [p.name for p in discover_files(str(workdir))]
    assert found == ["b.txt", "c.txt"]


# -- the whole flow --------------------------------------------------------


def _run_submit(workdir, submit_impl, log):
    """Drive cmd_submit with a stubbed network layer."""
    from unittest import mock

    from ipaapi import cli
    from ipaapi.auth import Credentials

    client = IPAClient(Credentials(access_token="x"), retries=0)
    args = cli.build_parser().parse_args(
        [
            "submit", str(workdir), "--ID", "0:ensembl", "--FC", "1:logratio",
            "--project", "P05", "--log-file", log,
        ]
    )
    with mock.patch.object(IPAClient, "submit", submit_impl), mock.patch.object(
        cli, "_client", lambda _: client
    ):
        return cli.cmd_submit(args)


def test_quota_stops_the_run_and_leaves_the_rest(workdir):
    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")
    calls = {"n": 0}

    def submit(self, dataset, project, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise QuotaExceededError("quota exceeded", status_code=403, body="quota")
        return ["43595001"]

    _run_submit(workdir, submit, log)

    filed = sorted(p.name for p in (workdir / SUBMITTED_DIRNAME).iterdir())
    assert filed == ["a.txt"]
    # The two the allowance did not cover are untouched, ready for next time.
    assert (workdir / "b.txt").exists()
    assert (workdir / "c.txt").exists()
    assert not (workdir / FAILED_DIRNAME).exists()


def test_a_server_rejection_files_the_file_as_failed(workdir):
    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")

    def submit(self, dataset, project, **kw):
        if dataset.name == "b":
            raise SubmissionError("Invalid geneidtype", status_code=400, body="bad")
        return ["43595001"]

    _run_submit(workdir, submit, log)

    assert sorted(p.name for p in (workdir / SUBMITTED_DIRNAME).iterdir()) == [
        "a.txt",
        "c.txt",
    ]
    assert (workdir / FAILED_DIRNAME / "b.txt").exists()
    note = (workdir / FAILED_DIRNAME / "b.txt.error.txt").read_text()
    assert "Invalid geneidtype" in note


def test_a_rerun_only_sees_what_is_left(workdir):
    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")
    calls = {"n": 0}

    def submit(self, dataset, project, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise QuotaExceededError("quota exceeded", status_code=403, body="quota")
        return ["43595001"]

    _run_submit(workdir, submit, log)
    remaining = [p.name for p in discover_files(str(workdir))]
    assert remaining == ["b.txt", "c.txt"]


# -- malformed requests are the command's fault, not the file's -------------

HTML_ERROR = (
    '<html>\n<head>\n    <title>Error | IPA\n    </title>\n'
    '</head>\n<body>\n<div class="ipaheader">Error</div>\n'
    "If you continue to experience this problem, please Send a Report\n"
    "</body></html>"
)


def test_html_response_is_a_malformed_request_not_a_data_problem():
    from ipaapi.client import looks_like_html
    from ipaapi.errors import MalformedRequestError

    assert looks_like_html(HTML_ERROR)
    with pytest.raises(MalformedRequestError):
        IPAClient._parse_analysis_ids(FakeResponse(HTML_ERROR, 200), expected=1)


def test_malformed_request_error_names_the_likely_parameters():
    from ipaapi.errors import MalformedRequestError

    try:
        IPAClient._parse_analysis_ids(FakeResponse(HTML_ERROR, 200), expected=1)
    except MalformedRequestError as exc:
        message = str(exc)
    assert "--reference-set" in message
    assert "Error | IPA" in message          # the page title is surfaced
    assert "every file in this batch" in message


def test_plain_text_rejections_are_still_ordinary_failures():
    from ipaapi.client import looks_like_html
    from ipaapi.errors import MalformedRequestError

    assert not looks_like_html("Invalid geneidtype")
    try:
        IPAClient._parse_analysis_ids(FakeResponse("Invalid geneidtype", 400), expected=1)
    except SubmissionError as exc:
        assert not isinstance(exc, MalformedRequestError)


def test_a_malformed_request_leaves_every_file_alone(workdir):
    """A bad parameter must not quarantine good data."""
    import tempfile

    from ipaapi.errors import MalformedRequestError

    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")

    def submit(self, dataset, project, **kw):
        raise MalformedRequestError("bad parameter", status_code=200, body=HTML_ERROR)

    _run_submit(workdir, submit, log)

    assert not (workdir / SUBMITTED_DIRNAME).exists()
    assert not (workdir / FAILED_DIRNAME).exists()
    for name in ("a.txt", "b.txt", "c.txt"):
        assert (workdir / name).exists()
