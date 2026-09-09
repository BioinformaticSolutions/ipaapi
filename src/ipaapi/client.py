"""High-level client for the IPA analysis API."""

from __future__ import annotations

import re
import time
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence, Union

import requests
from requests.adapters import HTTPAdapter

from . import _payload
from .auth import Credentials, TokenCache, login
from .dataset import Dataset
from .errors import (
    AnalysisError,
    AnalysisRefusedError,
    IPAError,
    MalformedRequestError,
    QuotaExceededError,
    ResultsUnavailableError,
    ServiceUnavailableError,
    SubmissionError,
)
from .models import AnalysisStatus, ReferenceSet

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

__all__ = [
    "IPAClient",
    "AnalysisResults",
    "QUOTA_PATTERNS",
    "looks_like_quota",
    "looks_like_html",
    "looks_like_outage",
    "html_error_text",
]

_ENTITY_ENDPOINTS = {
    "CANONICAL_PATHWAY": ("allCanonicalPathways", "pathways"),
    "UPSTREAM_REGULATOR": ("allUpstreamRegulators", "regulators"),
    "BIOFUNCTION": ("allBioFunctions", "functions"),
}

#: Analysis IDs come back as a bare comma-separated string; this is the shape
#: of a plausible ID, used to tell a real response from an error page.
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


@dataclass
class AnalysisResults:
    """Scored results for one analysis, as three DataFrames.

    Attributes:
        analysis_id: The analysis these results belong to.
        canonical_pathways: Canonical pathway scores, sorted by p-value.
        upstream_regulators: Upstream regulator scores, sorted by p-value.
        bio_functions: Diseases and biological function scores, sorted by p-value.
    """

    analysis_id: str
    canonical_pathways: "pd.DataFrame"
    upstream_regulators: "pd.DataFrame"
    bio_functions: "pd.DataFrame"

    def __iter__(self):
        """Unpack as ``cp, ur, df = results`` for parity with the demo code."""
        yield self.canonical_pathways
        yield self.upstream_regulators
        yield self.bio_functions

    def summary(self) -> str:
        return (
            f"analysis {self.analysis_id}: "
            f"{len(self.canonical_pathways)} canonical pathways, "
            f"{len(self.upstream_regulators)} upstream regulators, "
            f"{len(self.bio_functions)} diseases & functions"
        )


class IPAClient:
    """Submit datasets to IPA and track the resulting analyses.

    Args:
        credentials: Token and session context from :func:`ipaapi.auth.login`.
        timeout: Per-request timeout in seconds. Submissions carry the whole
            dataset in the body, so the default is generous.
        retries: Retry count for idempotent GET requests. Submissions are never
            retried automatically, since a retried POST could create a duplicate
            analysis.
        session: Optional pre-configured :class:`requests.Session`, e.g. one
            carrying proxy settings.

    Example:
        >>> client = IPAClient.login()                     # doctest: +SKIP
        >>> ids = client.submit(dataset, project="MyProject")   # doctest: +SKIP
        >>> client.wait_for(ids)                           # doctest: +SKIP
    """

    def __init__(
        self,
        credentials: Credentials,
        timeout: float = 600.0,
        retries: int = 3,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.session = session or requests.Session()
        if retries:
            adapter = HTTPAdapter(max_retries=self._retry_policy(retries))
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

    @staticmethod
    def _retry_policy(retries: int):
        from urllib3.util.retry import Retry

        return Retry(
            total=retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )

    @classmethod
    def login(cls, cache: Optional[TokenCache] = None, **kwargs) -> "IPAClient":
        """Run the browser OAuth flow and return a ready client.

        Accepts every keyword :func:`ipaapi.auth.login` takes.
        """
        return cls(credentials=login(cache=cache, **kwargs))

    # -- plumbing ----------------------------------------------------------

    @property
    def host(self) -> str:
        return self.credentials.host

    @property
    def application_name(self) -> str:
        return self.credentials.application_name

    def _url(self, path: str) -> str:
        return f"https://{self.host}/{path.lstrip('/')}"

    def _get(self, path: str, **params) -> requests.Response:
        params.setdefault("applicationname", self.application_name)
        try:
            return self.session.get(
                self._url(path),
                headers=self.credentials.auth_header,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IPAError(f"Request to {path} failed: {exc}") from exc

    # -- submission --------------------------------------------------------

    def submit(
        self,
        dataset: Dataset,
        project: str,
        analysis_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        reference_set: Optional[Union[ReferenceSet, str]] = None,
        ipa_view: str = "none",
    ) -> List[str]:
        """Upload *dataset* into *project* and start analyses on it.

        IPA's ``multiobsanalysis`` endpoint performs both halves of the workflow
        in a single call: the dataset is created inside the named project using
        the column mapping supplied, and an analysis is then launched for each
        observation in it. One analysis ID is returned per observation.

        Args:
            dataset: A validated :class:`~ipaapi.dataset.Dataset`.
            project: Destination IPA project. IPA creates it if it does not
                already exist.
            analysis_name: Base name for the analyses. Defaults to the dataset
                name.
            dataset_name: Name for the uploaded dataset. Defaults to
                ``dataset.name``, which itself defaults to the source filename.
            reference_set: Background analyses are scored against. Defaults
                to ``None``, which omits the parameter so IPA applies its own
                default -- confirmed to produce real p-values and FDR. Pass
                ``ReferenceSet.DATASET`` to score against the uploaded genes
                instead, which is appropriate only when the upload is a
                complete measured transcriptome rather than a filtered list.
            ipa_view: IPA view parameter; ``"none"`` unless you have a reason.

        Returns:
            Analysis IDs, one per observation, in mapping order.

        Raises:
            SubmissionError: If IPA rejects the submission or returns something
                that is not a list of analysis IDs.
        """
        effective_dataset_name = dataset_name or dataset.name or "dataset"
        if reference_set is None:
            reference = None
        elif isinstance(reference_set, ReferenceSet):
            reference = reference_set.value
        else:
            reference = str(reference_set)

        pairs = _payload.build_submission_pairs(
            frame=dataset.frame,
            mapping=dataset.mapping,
            application_name=self.application_name,
            project_name=project,
            dataset_name=effective_dataset_name,
            analysis_name=analysis_name,
            reference_set=reference,
            ipa_view=ipa_view,
        )
        body = _payload.encode_submission(pairs)

        try:
            response = self.session.post(
                self._url("/pa/api/v2/multiobsanalysis"),
                headers={
                    **self.credentials.auth_header,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=body.encode("utf-8"),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SubmissionError(f"Submission request failed: {exc}") from exc

        return self._parse_analysis_ids(response, len(dataset.mapping.observations))

    @staticmethod
    def _parse_analysis_ids(response: requests.Response, expected: int) -> List[str]:
        text = (response.text or "").strip()
        if response.status_code != 200:
            _raise_submission_error(
                f"IPA rejected the submission (HTTP {response.status_code}).",
                response.status_code,
                text,
            )
        ids = [part.strip() for part in text.split(",") if part.strip()]
        if not ids or not all(_ID_RE.match(i) for i in ids):
            _raise_submission_error(
                "IPA returned a response that does not look like analysis IDs. "
                "This usually means the request was malformed, the allowance is "
                "exhausted, or the token lacks permission for the project.",
                response.status_code,
                text,
            )
        if len(ids) != expected:
            # Not fatal -- surface it rather than silently mismatching.
            print(
                f"Warning: submitted {expected} observation(s) but IPA returned "
                f"{len(ids)} analysis ID(s): {', '.join(ids)}"
            )
        return ids

    # -- status ------------------------------------------------------------

    def status(self, analysis_id: str) -> AnalysisStatus:
        """Return the current status of one analysis."""
        response = self._get("/pa/api/v2/analysisstatus", analysisuid=analysis_id)
        if response.status_code != 200:
            raise IPAError(
                f"Status check for {analysis_id} failed (HTTP {response.status_code}): "
                f"{(response.text or '')[:300]}"
            )
        return AnalysisStatus.from_code(response.text)

    def wait_for(
        self,
        analysis_ids: Union[str, Sequence[str]],
        interval: float = 30.0,
        timeout: Optional[float] = 3600.0,
        progress: bool = True,
        raise_on_failure: bool = False,
    ) -> Dict[str, AnalysisStatus]:
        """Poll until every analysis reaches a terminal state.

        Args:
            analysis_ids: One ID or a sequence of them.
            interval: Seconds between polling rounds.
            timeout: Overall budget in seconds; ``None`` waits indefinitely.
            progress: Print a line per state change.
            raise_on_failure: Raise :class:`AnalysisError` if any analysis ends
                failed or canceled, instead of just reporting it.

        Returns:
            Mapping of analysis ID to its final (or last observed) status.
        """
        ids = [analysis_ids] if isinstance(analysis_ids, str) else list(analysis_ids)
        if not ids:
            return {}

        deadline = None if timeout is None else time.monotonic() + timeout
        final: Dict[str, AnalysisStatus] = {}
        last_seen: Dict[str, AnalysisStatus] = {}

        while True:
            for analysis_id in ids:
                if analysis_id in final:
                    continue
                state = self.status(analysis_id)
                if progress and last_seen.get(analysis_id) is not state:
                    print(f"analysis {analysis_id}: {state.name.lower()}")
                last_seen[analysis_id] = state
                if state.is_terminal:
                    final[analysis_id] = state

            if len(final) == len(ids):
                break
            if deadline is not None and time.monotonic() >= deadline:
                if progress:
                    pending = [i for i in ids if i not in final]
                    print(
                        f"Gave up after {timeout:g}s; still running: {', '.join(pending)}"
                    )
                break
            time.sleep(interval)

        result = {i: final.get(i, last_seen.get(i, AnalysisStatus.IN_PROGRESS)) for i in ids}

        if raise_on_failure:
            bad = {i: s for i, s in result.items() if not s.succeeded}
            if bad:
                detail = ", ".join(f"{i}={s.name.lower()}" for i, s in bad.items())
                raise AnalysisError(f"Analyses did not succeed: {detail}")
        return result

    def submit_and_wait(
        self,
        dataset: Dataset,
        project: str,
        interval: float = 30.0,
        timeout: Optional[float] = 3600.0,
        **submit_kwargs,
    ) -> Dict[str, AnalysisStatus]:
        """Submit *dataset* and block until the analyses finish."""
        ids = self.submit(dataset, project=project, **submit_kwargs)
        return self.wait_for(ids, interval=interval, timeout=timeout)

    # -- results -----------------------------------------------------------

    def results(self, analysis_id: str) -> AnalysisResults:
        """Fetch scored results for a completed analysis.

        .. note::
           Programmatic result retrieval is a commercial IPA add-on. Without
           that licence these endpoints return an error and
           :class:`~ipaapi.errors.ResultsUnavailableError` is raised; the
           analysis itself is unaffected and can still be opened in IPA.
        """
        import pandas as pd

        frames = {}
        for entity in ("CANONICAL_PATHWAY", "UPSTREAM_REGULATOR", "BIOFUNCTION"):
            records = self._entity_scores(analysis_id, entity)
            frames[entity] = pd.DataFrame(records)

        return AnalysisResults(
            analysis_id=analysis_id,
            canonical_pathways=frames["CANONICAL_PATHWAY"],
            upstream_regulators=frames["UPSTREAM_REGULATOR"],
            bio_functions=frames["BIOFUNCTION"],
        )

    def _entity_scores(self, analysis_id: str, entity: str) -> List[dict]:
        endpoint, json_key = _ENTITY_ENDPOINTS[entity]
        url = f"/pa/ipa/analysisResults/{endpoint}/{self.application_name}/{analysis_id}"
        try:
            response = self.session.get(
                self._url(url),
                headers=self.credentials.auth_header,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ResultsUnavailableError(
                f"Could not fetch {entity.lower()} results for {analysis_id}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ResultsUnavailableError(
                f"Could not fetch {entity.lower()} results for {analysis_id} "
                f"(HTTP {response.status_code}). Programmatic result retrieval "
                "requires the commercial IPA add-on licence."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResultsUnavailableError(
                f"Result response for {analysis_id} was not valid JSON."
            ) from exc

        records = []
        for item in payload.get(json_key, []) or []:
            row = {}
            for key, value in item.items():
                if isinstance(value, str) and key == "name":
                    value = value.replace("+", " ")
                elif isinstance(value, bool):
                    pass
                elif isinstance(value, (int, float)):
                    value = float(value)
                row[key] = value
            records.append(row)

        records.sort(key=lambda r: _sort_key(r.get("pvalue", r.get("Pvalue"))))
        return records

    # -- reports -----------------------------------------------------------

    def report_url(self, analysis_id: str) -> str:
        """Return the IPA Interpret link for *analysis_id*."""
        url = f"/pa/ipa/analysisResults/interpretLink/{self.application_name}/{analysis_id}"
        try:
            response = self.session.get(
                self._url(url),
                headers=self.credentials.auth_header,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IPAError(f"Could not fetch the report URL for {analysis_id}: {exc}") from exc

        if response.status_code != 200:
            body = (response.text or "").strip()[:1000]
            raise IPAError(
                f"Could not fetch the report URL for {analysis_id} "
                f"(HTTP {response.status_code}) from {url}."
                + (f"\nIPA said: {body!r}" if body else "\nThe response was empty.")
                + "\nInterpret links may require the commercial IPA add-on; the "
                "analysis itself is unaffected and can be opened in IPA directly."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            body = (response.text or "").strip()[:1000]
            raise IPAError(
                f"Report URL response for {analysis_id} was not JSON: {body!r}"
            ) from exc
        link = payload.get("link")
        if not link:
            raise IPAError(
                f"No 'link' field in the report response for {analysis_id}. "
                f"Response keys: {sorted(payload)!r}"
            )
        return link

    def open_report(self, analysis_id: str) -> str:
        """Open the Interpret report in a browser and return its URL."""
        url = self.report_url(analysis_id)
        webbrowser.open(url)
        return url

    def report_urls(self, analysis_ids: Iterable[str]) -> Dict[str, Optional[str]]:
        """Fetch report URLs for several analyses, tolerating individual failures."""
        out: Dict[str, Optional[str]] = {}
        for analysis_id in analysis_ids:
            try:
                out[analysis_id] = self.report_url(analysis_id)
            except IPAError as exc:
                print(f"Warning: {exc}")
                out[analysis_id] = None
        return out


#: Phrases that indicate an exhausted allowance rather than a broken request.
#:
#: **Confirmed wording**, observed from a live rejection::
#:
#:     Unable to run analysis: Analysis limit exceeded
#:
#: The remaining patterns are still guesses at other phrasings IPA might use.
#: Matching is deliberately broad and case-insensitive across the whole body,
#: because the cost of a false positive is mild -- the file is left in place for
#: the next run rather than quarantined -- while a false negative would file a
#: retryable submission under ``failed/``. The body is always printed, so a
#: misclassification stays visible.
QUOTA_PATTERNS = (
    "analysis limit exceeded",  # confirmed
    "quota",
    "allowance",
    "exceeded",
    "limit reached",
    "usage limit",
    "too many analyses",
    "no analyses remaining",
    "insufficient credits",
)


def looks_like_quota(status_code: Optional[int], body: str) -> bool:
    """Whether a rejection looks like an exhausted allowance.

    HTTP 429 is treated as a quota response outright; otherwise the body is
    searched for any of :data:`QUOTA_PATTERNS`.
    """
    if status_code == 429:
        return True
    haystack = (body or "").lower()
    return any(pattern in haystack for pattern in QUOTA_PATTERNS)


def looks_like_html(body: str) -> bool:
    """Whether the response is an HTML page rather than the expected plain text.

    The submission endpoint answers with a bare comma-separated list of IDs. An
    HTML page means the request was rejected before reaching the analysis logic
    -- a malformed parameter rather than bad data.
    """
    head = (body or "").lstrip()[:200].lower()
    return head.startswith(("<html", "<!doctype html", "<?xml")) or "<html" in head


#: IPA's error pages sandwich the actual reason between support boilerplate
#: above and site chrome below. Stripping both is what makes it readable.
_BOILERPLATE = re.compile(
    r"If you continue to experience this problem.*?1-650-381-5111\.?",
    re.IGNORECASE | re.DOTALL,
)
_PAGE_FOOTER = re.compile(
    r"About QIAGEN Bioinformatics.*$|\(c\)\s*\d{4}-\d{4}\s*QIAGEN.*$"
    r"|&copy;\s*\d{4}-\d{4}\s*QIAGEN.*$",
    re.IGNORECASE | re.DOTALL,
)


def html_error_text(body: str) -> str:
    """Return the readable message from an IPA HTML error page.

    Tags are stripped, entities collapsed and the support boilerplate removed,
    because the sentence that actually says what went wrong comes *after* it.
    Truncating from the front -- as this used to -- discards precisely the part
    worth reading.
    """
    text = re.sub(r"<[^>]+>", " ", body or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = _BOILERPLATE.sub(" ", text)
    text = _PAGE_FOOTER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # "Error | IPA Error" prefixes carry no information.
    text = re.sub(r"^(Error\s*\|\s*IPA\s*)+(Error\s*)*", "", text).strip()
    return text


def _summarise_html_error(body: str) -> str:
    """Describe an HTML error page, keeping the tail where the reason lives."""
    text = html_error_text(body)
    if not text:
        return "an HTML error page with no readable content"
    if len(text) > 1500:
        # Keep both ends rather than losing the conclusion.
        text = f"{text[:500]} [...] {text[-900:]}"
    return repr(text)


#: IPA names the offending value in its error page; catching that turns a
#: generic "something was wrong" into an actionable message.
_UNKNOWN_ID_TYPE = re.compile(r"Unknown\s+GeneId\s+Type\s*\(([^)]*)\)", re.IGNORECASE)


def _parameter_hint(body: str):
    """Return ``(headline, detail)`` naming the parameter IPA objected to.

    The headline is deliberately short and unambiguous, because it is the line
    a reader scanning a wall of error text will actually take in.
    """
    match = _UNKNOWN_ID_TYPE.search(body or "")
    if match:
        rejected = match.group(1).strip()
        return (
            f"REJECTED: IPA does not recognise the gene ID type {rejected!r}.",
            "That is the --ID flag: --ID COLUMN:TYPE. Confirmed values: 'ensembl' "
            "for Ensembl gene IDs, 'hugo' for human gene symbols. The vocabulary "
            "is undocumented and unobvious -- 'genesymbol' and 'Gene Symbol' are "
            "both rejected, so it is neither the compound word nor the desktop "
            "client's display label. IPA names whatever value it rejects, so "
            "candidates can be tried one at a time; examples/probe_geneidtype.py "
            "does that.\n"
            "A type IPA *accepts* creates a real analysis and consumes allowance, "
            "so probe with a small file.",
        )
    return (
        "REJECTED: IPA would not accept one of the submission parameters.",
        "A long observation name is the most likely cause, and the hardest to "
        "guess: IPA rejects one and reports it as an outage page rather than a "
        "parameter error. Anything past roughly 65 characters is at risk, and "
        "since the observation name defaults to the filename, long filenames "
        "trip it. A long *dataset* name is fine -- only the observation matters. "
        "Since 1.2.0 the name is shortened automatically, so an older build is "
        "worth ruling out first (ipaapi --version).\n"
        "Failing that, --reference-set and the --ID type are the other "
        "candidates. The response below is the only description IPA gives.",
    )


#: IPA says this when the request reached the analysis logic but could not be
#: run -- as opposed to being rejected on a parameter.
_UNABLE_TO_RUN = re.compile(r"Unable to run analysis", re.IGNORECASE)

#: IPA's maintenance/outage page. Nothing to do with the request, so reporting
#: it as a parameter error sends people rewriting a correct command.
_OUTAGE = re.compile(
    r"currently unavailable"
    r"|experiencing technical difficulties"
    r"|technical difficulties"
    r"|temporarily unavailable"
    r"|service unavailable"
    r"|try again later",
    re.IGNORECASE,
)


def looks_like_outage(status_code: Optional[int], body: str) -> bool:
    """Whether the response is IPA being down rather than rejecting the request."""
    if status_code in (502, 503, 504):
        return True
    return bool(_OUTAGE.search(body or ""))


def _raise_submission_error(message: str, status_code: Optional[int], body: str):
    """Raise the most specific submission error the response supports."""
    excerpt = body[:2000]

    # Checked before the HTML branch: an exhausted allowance delivered as an
    # error page is still a quota problem, not a malformed request.
    if looks_like_quota(status_code, body):
        detail = html_error_text(body) if looks_like_html(body) else excerpt
        raise QuotaExceededError(
            f"REJECTED: the analysis allowance appears to be exhausted.\n\n"
            f"IPA said: {detail!r}",
            status_code=status_code,
            body=excerpt,
        )

    if looks_like_outage(status_code, body):
        detail = html_error_text(body) if looks_like_html(body) else excerpt
        raise ServiceUnavailableError(
            "REJECTED: IPA appears to be down or having trouble.\n\n"
            "This is not a problem with your command or your data -- IPA "
            "returned its maintenance page rather than processing the request. "
            "Nothing was submitted and no files were moved, so re-running the "
            "same command later will pick up exactly where it stopped.\n\n"
            f"IPA said: {detail!r}",
            status_code=status_code,
            body=excerpt,
        )

    if looks_like_html(body) and _UNABLE_TO_RUN.search(body):
        raise AnalysisRefusedError(
            "REJECTED: IPA accepted the request but would not start the "
            "analysis.\n\n"
            "This is not a parameter problem -- the request reached IPA's "
            "analysis logic, which then declined to run it. The usual causes "
            "are an exhausted analysis allowance, a capacity limit, or a "
            "transient fault on IPA's side; the dataset and the command line "
            "are probably fine. The remaining files have been left in place, "
            "so re-running the same command later resumes.\n\n"
            f"IPA said: {_summarise_html_error(body)}",
            status_code=status_code,
            body=excerpt,
        )

    if looks_like_html(body):
        # Lead with what IPA actually objected to. Burying it under the
        # explanation invites the reader to skim and conclude it worked.
        headline, detail = _parameter_hint(body)
        raise MalformedRequestError(
            headline
            + "\n\nThis file was NOT submitted.\n\n"
            + detail
            + "\n\nIPA answered with an HTML error page where the API returns "
            "plain text, which means the request was rejected before reaching "
            "the analysis logic -- so any remaining file would fail the same "
            "way.\n"
            f"Full response: {_summarise_html_error(body)}",
            status_code=status_code,
            body=excerpt,
        )

    detail = f"{message}\nIPA said: {excerpt!r}" if excerpt else message
    if looks_like_quota(status_code, body):
        raise QuotaExceededError(detail, status_code=status_code, body=excerpt)
    raise SubmissionError(detail, status_code=status_code, body=excerpt)


def _sort_key(value) -> float:
    """Sort p-values ascending, pushing missing or unparseable values last."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("inf")
    return float("inf") if number != number else number
