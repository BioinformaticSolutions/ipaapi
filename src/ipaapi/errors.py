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


class AnalysisError(IPAError):
    """An analysis finished in a non-successful terminal state, or never finished."""


class ResultsUnavailableError(IPAError):
    """Analysis results could not be retrieved.

    Programmatic result retrieval is a commercial IPA add-on. If the account in
    use is not licensed for it, the results endpoints return an error and this
    exception is raised. Submission and status checking are unaffected.
    """
