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
from datetime import datetime
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
        timestamp: Local time with UTC offset, ISO 8601, to the second.
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
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append(
    records: Sequence[SubmissionRecord], path: Optional[str] = None
) -> Optional[str]:
    """Append *records* to the log, creating it with a header if needed.

    Returns the path written to, or ``None`` if the write failed. Logging is
    best-effort: a full disk should not lose an analysis that IPA has already
    accepted, so failures are reported and swallowed.
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
        print(
            f"Warning: could not write the submission log at {path!r} ({exc}).\n"
            "  The analyses were submitted, but their IDs are only in this "
            "terminal -- save them.\n"
            f"  Set a writable location with --log-file PATH or "
            f"export {LOG_FILE_ENV}=$HOME/ipaapi-submissions.tsv"
        )
        return None


def read(path: Optional[str] = None) -> List[dict]:
    """Read the log back, oldest first. A missing log reads as empty."""
    path = path or default_log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]
    except OSError as exc:
        print(f"Warning: could not read the submission log at {path!r} ({exc}).")
        return []
