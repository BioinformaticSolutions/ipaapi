"""A local record of everything submitted through this package.

IPA's API offers no way to list the analyses on an account: every endpoint
takes an analysis ID you must already hold. Lose the terminal output of a
submission and the ID is gone, recoverable only by hunting through the IPA
client by eye.

So the package keeps its own log. Each submitted analysis appends one
timestamped row to a tab-separated file, which ``ipaapi history`` reads back.
This covers work done through this tool only -- it cannot recover analyses
submitted from the IPA desktop client.

The format is deliberately boring: a TSV with a header, appended a line at a
time, so it survives interruption, is readable in any spreadsheet, and can be
grepped when all else fails.
"""

from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence

__all__ = [
    "SubmissionRecord",
    "default_log_path",
    "append",
    "read",
    "FIELDS",
    "LOG_FILE_ENV",
]

FIELDS = (
    "timestamp",
    "analysis_id",
    "project",
    "dataset_name",
    "observation",
    "source_file",
    "application_name",
    "host",
)


#: Overrides the log location, for hosts where the home directory is not
#: writable. Mirrors ``IPAAPI_TOKEN_FILE``.
LOG_FILE_ENV = "IPAAPI_LOG_FILE"


def default_log_path() -> str:
    """Where the log lives unless told otherwise.

    ``IPAAPI_LOG_FILE`` wins, then ``XDG_STATE_HOME``, then
    ``~/.local/state/ipaapi/submissions.tsv``.
    """
    override = os.environ.get(LOG_FILE_ENV)
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "ipaapi", "submissions.tsv")


@dataclass
class SubmissionRecord:
    """One submitted analysis.

    Attributes:
        timestamp: UTC, ISO 8601, to the second.
        analysis_id: The ID IPA returned.
        project: Project the dataset was uploaded into.
        dataset_name: Dataset name as IPA sees it.
        observation: Observation the analysis covers.
        source_file: Absolute path of the file submitted.
        application_name: ``applicationname`` used, which scopes the analysis.
        host: IPA host it was submitted to.
    """

    analysis_id: str
    project: str
    dataset_name: str = ""
    observation: str = ""
    source_file: str = ""
    application_name: str = ""
    host: str = ""
    timestamp: str = field(default_factory=lambda: _now())

    def as_row(self) -> List[str]:
        data = asdict(self)
        return [str(data.get(name, "")) for name in FIELDS]


def _now() -> str:
    """UTC, ISO 8601, to the second.

    Local offsets were written here, and the log is compared and sorted
    lexicographically -- by ``read``'s "oldest first" claim and by ``--since``.
    Two rows forty minutes apart across the autumn clock change sorted in the
    wrong order, and ``--since`` returned exactly the row it should have
    excluded. The same inversion is permanent, not annual, whenever two
    machines in different zones share one log, which IPAAPI_LOG_FILE on an
    exported filesystem invites. UTC sorts correctly everywhere.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(
    records: Sequence[SubmissionRecord],
    path: Optional[str] = None,
    quiet: bool = False,
) -> Optional[str]:
    """Append *records* to the log, creating it with a header if needed.

    Returns the path written to, or ``None`` if the write failed. Logging is
    best-effort: a full disk should not lose an analysis that IPA has already
    accepted, so failures are reported and swallowed.

    *quiet* suppresses the failure message. The CLI appends once per file, so
    an unwritable log would otherwise print the same four-line warning for
    every file in the batch and bury the run's own output.
    """
    if not records:
        return None
    path = path or default_log_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
            if not exists:
                writer.writerow(FIELDS)
            for record in records:
                writer.writerow(record.as_row())
        return path
    except OSError as exc:
        if quiet:
            return None
        print(
            f"Warning: could not write the submission log at {path!r} ({exc}).\n"
            "  The analyses were submitted, but their IDs are only in this "
            "terminal -- save them.\n"
            f"  Set a writable location with --log-file PATH or "
            f"export {LOG_FILE_ENV}=$HOME/ipaapi-submissions.tsv"
        )
        return None


def _decode(path: str) -> str:
    """Decode the log, falling back per LINE rather than for the whole file.

    Choosing one encoding from a single failure was too blunt: a log truncated
    mid-character -- a kill during the append, a full disk -- made UTF-8 fail,
    so every correctly written non-ASCII name in the file was re-read as cp1252
    and silently mojibaked. Unlike a replacement character, that damage looks
    like ordinary text, and ``_already_submitted`` matches dataset names
    exactly, so the duplicate guard stopped seeing rows it had written itself.

    Line by line, one damaged row costs only itself.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    out = []
    for line in raw.split(b"\n"):
        for encoding in ("utf-8", "cp1252"):
            try:
                out.append(line.decode(encoding))
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover - cp1252 decodes almost anything
            out.append(line.decode("utf-8", errors="replace"))
    return "\n".join(out)


def read(path: Optional[str] = None) -> List[dict]:
    """Read the log back, oldest first. A missing or damaged log reads as empty.

    Two failures used to escape as tracebacks, and neither stayed local to
    ``ipaapi history``: ``_already_submitted`` calls this for every dataset on
    the normal submit path, so an unreadable log blocked submission outright --
    the opposite of the best-effort contract ``append`` documents.

    A byte that is not UTF-8 raised ``UnicodeDecodeError``, which is a
    ``ValueError`` and so slipped past the ``OSError`` guard. Excel's
    tab-delimited export on Windows writes cp1252, and this module advertises
    the file as spreadsheet-readable. Undecodable bytes are now replaced.

    A truncated final row -- what an interrupted write or a full disk leaves --
    gave ``DictReader`` missing keys filled with ``None``, and ``.get(k, "")``
    returns that ``None`` because the key exists, so the caller's formatting
    raised ``TypeError``. ``restval`` now fills with the empty string.
    """
    path = path or default_log_path()
    if not os.path.exists(path):
        return []
    try:
        text = _decode(path)
        return [
            {
                str(k).lstrip("\ufeff"): ("" if v is None else v)
                for k, v in row.items()
                if k is not None
            }
            for row in csv.DictReader(text.splitlines(), delimiter="\t", restval="")
        ]
    except (OSError, ValueError, csv.Error) as exc:
        print(
            f"Warning: could not read the submission log at {path!r} ({exc}).\n"
            "  Treating it as empty. Submissions will still run, but the "
            "duplicate-name guard cannot see earlier runs."
        )
        return []
