"""Exception hierarchy for :mod:`ipaapi`.

Every error raised by this package derives from :class:`IPAError`, so callers
can catch that one type and be sure they have caught everything the library
raises deliberately.
"""

from __future__ import annotations

__all__ = [
    "IPAError",
    "AuthenticationError",
    "MappingError",
    "SubmissionError",
    "QuotaExceededError",
    "MalformedRequestError",
    "AnalysisRefusedError",
    "ServiceUnavailableError",
    "GatewayTimeoutError",
    "AnalysisError",
    "ResultsUnavailableError",
]


class IPAError(Exception):
    """Base class for all errors raised by :mod:`ipaapi`."""


class AuthenticationError(IPAError):
    """OAuth login failed, timed out, or produced an unusable token."""


class MappingError(IPAError):
    """A :class:`~ipaapi.mapping.ColumnMapping` is invalid or does not fit the data.

    Raised before any network call is made, so a bad mapping never costs an
    upload.
    """


class SubmissionError(IPAError):
    """The IPA server rejected an analysis submission."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class TokenRefusedError(AuthenticationError, SubmissionError):
    """IPA refused the token itself, rather than anything in the request.

    Inherits from both :class:`AuthenticationError` and :class:`SubmissionError`
    on purpose. It *is* an authentication problem, and code that wants to react
    to one -- by signing in again rather than reporting a bad file -- should
    catch it as such. But a submission that dies this way was still a submission
    failure, and callers written before this class existed catch
    ``SubmissionError`` around a submit. Being both keeps them working.

    Raised only on evidence that cannot mean anything else: an HTTP 401 or 403,
    or an OAuth error code delivered in a short non-HTML body. IPA's habit of
    answering HTTP 200 with an HTML page means a token rejection dressed up as
    a login page is *not* detected here, and deliberately so -- prose matching
    on a 200 is what once made a duplicate dataset name look like an outage.
    """

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        SubmissionError.__init__(self, message, status_code, body)


class QuotaExceededError(SubmissionError):
    """The account has no analyses left in its allowance.

    Distinguished from other submission failures because the file is fine and
    should be retried once the allowance resets -- unlike a malformed dataset,
    which will fail identically forever.

    .. warning::
       The exact response IPA sends when an allowance is exhausted is not
       documented, so detection is heuristic: see
       :data:`ipaapi.client.QUOTA_PATTERNS`. The raw response body is always
       reported so a misclassification is visible rather than silent.
    """


class MalformedRequestError(SubmissionError):
    """IPA rejected the request itself, not the data in it.

    Recognised by IPA answering with an HTML error page where the API contract
    is plain text: that means the request never reached the analysis logic, so
    a bad parameter -- not a bad file -- is at fault, and every other file in
    the batch would fail identically.

    Kept distinct so batch processing stops and leaves the files alone, rather
    than quarantining perfectly good data for a mistake in the command line.
    """


class ServiceUnavailableError(SubmissionError):
    """IPA is down or having trouble -- nothing to do with the request.

    Recognised by IPA's maintenance page ("currently unavailable", "technical
    difficulties", "try again later") or a 502/503/504. The command line and
    the data are fine; the only correct response is to wait and re-run.

    Kept distinct because the alternative -- reporting a service outage as a
    parameter error -- sends people rewriting a command that was never wrong.
    """


class GatewayTimeoutError(ServiceUnavailableError):
    """A gateway gave up waiting for IPA's backend.

    A subclass of :class:`ServiceUnavailableError` because the response is the
    same -- wait and re-run -- but worth naming separately, because a timeout
    differs from an outage in one way that matters: the request may have
    reached IPA and been acted on even though the answer never came back. A
    retry can therefore collide with a dataset the timed-out attempt created.

    Two causes are known. The submission may genuinely be slow to transfer, on
    a congested link or through a VPN. Or the request may be one IPA cannot
    process -- an over-long observation name has been seen to surface this way
    as well as via the maintenance page.
    """


class AnalysisRefusedError(SubmissionError):
    """IPA accepted the request but would not start the analysis.

    Distinguished from :class:`MalformedRequestError` by IPA saying "Unable to
    run analysis", which means the request reached the analysis logic rather
    than being rejected on a parameter. The dataset and the command line are
    therefore probably fine, and the cause is on IPA's side -- an exhausted
    allowance, a capacity limit, or a transient fault.

    Treated like a quota response for batch purposes: the run stops and the
    remaining files are left in place, since whatever stopped this submission
    will very likely stop the next one too.
    """


class AnalysisError(IPAError):
    """An analysis finished in a non-successful terminal state, or never finished."""


class ResultsUnavailableError(IPAError):
    """Analysis results could not be retrieved.

    Programmatic result retrieval is a commercial IPA add-on. If the account in
    use is not licensed for it, the results endpoints return an error and this
    exception is raised. Submission and status checking are unaffected.
    """
