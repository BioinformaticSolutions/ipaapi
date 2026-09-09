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

# Mirrors a real IPA error page, boilerplate and all.
HTML_ERROR = (
    '<html>\n<head>\n    <title>Error | IPA\n    </title>\n'
    '</head>\n<body>\n<div class="ipaheader">Error</div>\n'
    "If you continue to experience this problem, please Send a Report of this "
    "problem to Ingenuity Customer Support, or contact Ingenuity Customer "
    "Support at AdvancedGenomicsSupport@qiagen.com or 1-650-381-5111."
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
    # The "Error | IPA" chrome is stripped; what survives is the real text.
    assert "Error | IPA" not in message
    assert "Send a Report" not in message    # support boilerplate removed
    assert "any remaining file would fail the same way" in message


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


def test_a_mapping_that_fails_every_file_moves_nothing(workdir):
    """A wrong --FC type is the command's mistake; don't quarantine the data."""
    import tempfile
    from unittest import mock

    from ipaapi import cli
    from ipaapi.auth import Credentials

    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")
    client = IPAClient(Credentials(access_token="x"), retries=0)
    # Column 1 holds 2.0, which is a valid fold change but not a valid p-value.
    args = cli.build_parser().parse_args(
        [
            "submit", str(workdir), "--ID", "0:ensembl", "--FC", "1:pvalue",
            "--project", "P05", "--log-file", log,
        ]
    )
    with mock.patch.object(cli, "_client", lambda _: client):
        code = cli.cmd_submit(args)

    assert code == 1
    assert not (workdir / FAILED_DIRNAME).exists()
    assert not (workdir / SUBMITTED_DIRNAME).exists()
    for name in ("a.txt", "b.txt", "c.txt"):
        assert (workdir / name).exists()


def test_unknown_gene_id_type_is_named_and_pointed_at_the_right_flag():
    """IPA names the value it rejected; the error should say what to do with that."""
    from ipaapi.errors import MalformedRequestError

    body = (
        "<html><head><title>Error | IPA</title></head><body>"
        "Error &nbsp; If you continue to experience this problem, please Send a "
        "Report ... &nbsp; Unknown GeneId Type (genesymbol) </body></html>"
    )
    try:
        IPAClient._parse_analysis_ids(FakeResponse(body, 200), expected=1)
    except MalformedRequestError as exc:
        message = str(exc)
    assert message.startswith("REJECTED: IPA does not recognise the gene ID type")
    assert "'genesymbol'" in message
    assert "This file was NOT submitted" in message
    assert "--ID COLUMN:TYPE" in message
    assert "consumes allowance" in message      # probing is not free
    assert "--reference-set" not in message     # don't misdirect


def test_other_html_errors_keep_the_generic_advice():
    from ipaapi.errors import MalformedRequestError

    body = "<html><body>Something else went wrong</body></html>"
    try:
        IPAClient._parse_analysis_ids(FakeResponse(body, 200), expected=1)
    except MalformedRequestError as exc:
        assert str(exc).startswith("REJECTED:")
        assert "long observation name" in str(exc)


def test_advertised_id_types_come_from_the_documented_list():
    """Guesses in this list previously walked a user into a failed submission."""
    from ipaapi.cli import COMMON_ID_TYPES
    from ipaapi.models import GENE_ID_TYPES

    assert set(COMMON_ID_TYPES) <= set(GENE_ID_TYPES)
    # The values that cost a live submission to discover.
    assert "hugo" in GENE_ID_TYPES
    assert "genesymbol" not in GENE_ID_TYPES
    assert "hgnc" not in GENE_ID_TYPES
    # Species rides on the identifier type; there is no species parameter.
    assert GENE_ID_TYPES["mousesymeg"].lower().startswith("gene symbol -- mouse")


# -- IPA refusing to run an accepted request -------------------------------

UNABLE_TO_RUN = (
    "<html><head><title>Error | IPA</title></head><body>"
    "<div>Error</div> If you continue to experience this problem, please "
    "Send a Report of this problem to Ingenuity Customer Support, or contact "
    "Ingenuity Customer Support at AdvancedGenomicsSupport@qiagen.com or "
    "1-650-381-5111. &nbsp; Unable to run analysis: Analysis could not be "
    "created</body></html>"
)


def test_unable_to_run_is_not_treated_as_a_parameter_problem():
    """"Unable to run analysis" means the request reached the analysis logic."""
    from ipaapi.errors import AnalysisRefusedError, MalformedRequestError

    try:
        IPAClient._parse_analysis_ids(FakeResponse(UNABLE_TO_RUN, 200), expected=1)
    except MalformedRequestError:
        raise AssertionError("classified as a malformed request")
    except AnalysisRefusedError as exc:
        message = str(exc)
    assert "would not start the analysis" in message
    assert "not a parameter problem" in message
    assert "left in place" in message


def test_the_reason_survives_the_boilerplate():
    """IPA puts its support boilerplate first and the reason last."""
    from ipaapi.client import html_error_text

    text = html_error_text(UNABLE_TO_RUN)
    assert "Unable to run analysis: Analysis could not be created" in text
    assert "1-650-381-5111" not in text        # boilerplate stripped
    assert not text.startswith("Error | IPA")  # chrome stripped


def test_a_quota_message_inside_an_html_page_is_still_a_quota_error():
    """Previously an allowance message delivered as HTML looked like a bad parameter."""
    from ipaapi.errors import QuotaExceededError

    body = UNABLE_TO_RUN.replace("could not be created", "quota exceeded")
    with pytest.raises(QuotaExceededError):
        IPAClient._parse_analysis_ids(FakeResponse(body, 200), expected=1)


def test_refusal_stops_the_batch_and_leaves_the_rest(workdir):
    import tempfile

    from ipaapi.errors import AnalysisRefusedError

    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")
    calls = {"n": 0}

    def submit(self, dataset, project, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AnalysisRefusedError("refused", status_code=200, body=UNABLE_TO_RUN)
        return ["43595871"]

    _run_submit(workdir, submit, log)

    assert sorted(p.name for p in (workdir / SUBMITTED_DIRNAME).iterdir()) == ["a.txt"]
    assert (workdir / "b.txt").exists() and (workdir / "c.txt").exists()
    assert not (workdir / FAILED_DIRNAME).exists()


def test_the_real_quota_rejection_is_classified_and_readable():
    """Verbatim from a live rejection, chrome and all."""
    body = (
        '<html><head><title>Error | IPA</title></head><body>'
        '<div class="ipaheader"></div>Error &nbsp; &nbsp; If you continue to '
        "experience this problem, please <b>Send a Report</b> of this problem to "
        "Ingenuity Customer Support, or contact Ingenuity Customer Support at "
        "AdvancedGenomicsSupport@qiagen.com or 1-650-381-5111. &nbsp; "
        "Unable to run analysis: Analysis limit exceeded "
        "<div>About QIAGEN Bioinformatics | Contact Us &copy;2000-2026 QIAGEN. "
        "All rights reserved.</div></body></html>"
    )
    from ipaapi.client import html_error_text

    with pytest.raises(QuotaExceededError, match="allowance appears to be exhausted"):
        IPAClient._parse_analysis_ids(FakeResponse(body, 200), expected=1)

    # The reason survives; the boilerplate above and the chrome below do not.
    text = html_error_text(body)
    assert text == "Unable to run analysis: Analysis limit exceeded"


# -- IPA being down is not a parameter problem ------------------------------

OUTAGE = (
    "<html><head><title>Error | IPA</title></head><body>"
    "The page you are looking for is currently unavailable. The IPA site might "
    "be experiencing technical difficulties. Please try the following: Click the "
    "Refresh button on your browser, or try again later.</body></html>"
)


def test_an_outage_page_is_not_blamed_on_the_parameters():
    """Reporting a service outage as a bad flag sends people rewriting a correct command."""
    from ipaapi.errors import MalformedRequestError, ServiceUnavailableError

    try:
        IPAClient._parse_analysis_ids(FakeResponse(OUTAGE, 200), expected=1)
    except MalformedRequestError:
        raise AssertionError("an outage was classified as a malformed request")
    except ServiceUnavailableError as exc:
        message = str(exc)
    assert "IPA appears to be down" in message
    assert "not a problem with your command" in message
    assert "--reference-set" not in message      # do not misdirect
    assert "re-running the same command later" in message


def test_gateway_errors_are_outages_too():
    from ipaapi.client import looks_like_outage

    assert looks_like_outage(503, "")
    assert looks_like_outage(502, "")
    assert looks_like_outage(504, "")
    assert not looks_like_outage(400, "Unknown GeneId Type (hgnc)")


def test_an_outage_leaves_every_file_in_place(workdir):
    import tempfile

    from ipaapi.errors import ServiceUnavailableError

    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")

    def submit(self, dataset, project, **kw):
        raise ServiceUnavailableError("down", status_code=200, body=OUTAGE)

    _run_submit(workdir, submit, log)

    assert not (workdir / SUBMITTED_DIRNAME).exists()
    assert not (workdir / FAILED_DIRNAME).exists()
    for name in ("a.txt", "b.txt", "c.txt"):
        assert (workdir / name).exists()


# -- duplicate dataset names ------------------------------------------------


def test_a_dataset_already_in_the_project_is_skipped_not_resubmitted(workdir):
    """IPA rejects a duplicate dataset name, reporting it as "page unavailable".

    The submission log already knows what went where, so the collision can be
    predicted instead of walked into -- and the file counts as done.
    """
    import tempfile
    from unittest import mock

    from ipaapi import cli, history
    from ipaapi.auth import Credentials

    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")
    history.append(
        [history.SubmissionRecord(analysis_id="43631092", project="P05",
                                  dataset_name="a")],
        path=log,
    )

    calls = []

    def submit(self, dataset, project, **kw):
        calls.append(dataset.name)
        return ["999"]

    client = IPAClient(Credentials(access_token="x"), retries=0)
    args = cli.build_parser().parse_args(
        ["submit", str(workdir), "--ID", "0:ensembl", "--FC", "1:logratio",
         "--project", "P05", "--log-file", log]
    )
    with mock.patch.object(IPAClient, "submit", submit), mock.patch.object(
        cli, "_client", lambda _: client
    ):
        cli.cmd_submit(args)

    # 'a' was already in P05, so it never reached IPA...
    assert "a" not in calls
    assert sorted(calls) == ["b", "c"]
    # ...but it still counts as done, so a re-run does not see it again.
    assert (workdir / SUBMITTED_DIRNAME / "a.txt").exists()


def test_force_resubmits_a_known_duplicate(workdir):
    import tempfile
    from unittest import mock

    from ipaapi import cli, history
    from ipaapi.auth import Credentials

    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")
    history.append(
        [history.SubmissionRecord(analysis_id="1", project="P05", dataset_name="a")],
        path=log,
    )
    calls = []

    def submit(self, dataset, project, **kw):
        calls.append(dataset.name)
        return ["999"]

    client = IPAClient(Credentials(access_token="x"), retries=0)
    args = cli.build_parser().parse_args(
        ["submit", str(workdir), "--ID", "0:ensembl", "--FC", "1:logratio",
         "--project", "P05", "--log-file", log, "--force"]
    )
    with mock.patch.object(IPAClient, "submit", submit), mock.patch.object(
        cli, "_client", lambda _: client
    ):
        cli.cmd_submit(args)

    assert "a" in calls


def test_the_same_name_in_a_different_project_is_not_a_collision(workdir):
    """IPA scopes dataset names per project, so a different project is fine."""
    import tempfile
    from unittest import mock

    from ipaapi import cli, history
    from ipaapi.auth import Credentials

    log = str(pathlib.Path(tempfile.mkdtemp()) / "log.tsv")
    history.append(
        [history.SubmissionRecord(analysis_id="1", project="OTHER", dataset_name="a")],
        path=log,
    )
    calls = []

    def submit(self, dataset, project, **kw):
        calls.append(dataset.name)
        return ["999"]

    client = IPAClient(Credentials(access_token="x"), retries=0)
    args = cli.build_parser().parse_args(
        ["submit", str(workdir), "--ID", "0:ensembl", "--FC", "1:logratio",
         "--project", "P05", "--log-file", log]
    )
    with mock.patch.object(IPAClient, "submit", submit), mock.patch.object(
        cli, "_client", lambda _: client
    ):
        cli.cmd_submit(args)

    assert "a" in calls
