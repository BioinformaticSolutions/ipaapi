"""Loading tabular datasets and pairing them with a :class:`ColumnMapping`."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union

from .errors import MappingError
from .mapping import ColumnMapping
from .models import MeasurementType

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

__all__ = ["Dataset", "load_table"]

PathLike = Union[str, "os.PathLike[str]"]


def _sniff_separator(path: PathLike, default: str = "\t", skip_rows: int = 0) -> str:
    """Guess the delimiter from the header line.

    *skip_rows* lines of preamble are stepped over first, so a comment or title
    line above the header does not get sniffed by mistake.
    """
    try:
        with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as fh:
            for _ in range(skip_rows):
                if fh.readline() == "":
                    raise MappingError(
                        f"{str(path)!r} has fewer than {skip_rows + 1} lines, so there "
                        "is no header row left after --skip-rows."
                    )
            sample = fh.readline()
    except OSError as exc:
        raise MappingError(f"Could not read {str(path)!r}: {exc}") from exc
    if not sample:
        raise MappingError(f"{str(path)!r} appears to be empty.")
    try:
        return csv.Sniffer().sniff(sample, delimiters="\t,;|").delimiter
    except csv.Error:
        return default


def load_table(
    path: PathLike,
    sep: Optional[str] = None,
    skip_rows: int = 0,
    **read_csv_kwargs,
) -> "pd.DataFrame":
    """Read a delimited text file into a DataFrame with a real header row.

    Unlike the original demo, which read the file with ``header=None`` and then
    treated row 0 as text, this keeps the header as column names so a
    :class:`ColumnMapping` can address columns by name.

    Args:
        path: File to read.
        sep: Field delimiter. Sniffed from the header line when omitted.
        skip_rows: Number of lines to discard before the header row, for files
            that carry a comment, title or provenance block above it.
        **read_csv_kwargs: Passed through to :func:`pandas.read_csv`.
    """
    import pandas as pd

    if skip_rows < 0:
        raise MappingError("skip_rows cannot be negative.")
    if sep is None:
        sep = _sniff_separator(path, skip_rows=skip_rows)
    read_csv_kwargs.setdefault("dtype", object)
    read_csv_kwargs.setdefault("encoding", "utf-8-sig")
    if skip_rows:
        read_csv_kwargs.setdefault("skiprows", skip_rows)
    try:
        frame = pd.read_csv(path, sep=sep, **read_csv_kwargs)
    except Exception as exc:  # pandas raises a wide variety of parse errors
        raise MappingError(f"Could not parse {str(path)!r} as a table: {exc}") from exc
    frame.columns = [str(c).strip() for c in frame.columns]

    if not skip_rows:
        _warn_if_header_looks_wrong(path, frame)
    return frame


#: Line prefixes that mark a comment in the formats these tables arrive in.
_COMMENT_PREFIXES = ("#", "//", ";", "!")


def _warn_if_header_looks_wrong(path: PathLike, frame: "pd.DataFrame") -> None:
    """Raise if the row taken as the header is obviously not one.

    A comment or title line above the real header is common, and the failure is
    otherwise silent and confusing: the delimiter gets sniffed from the comment,
    the comment becomes the column names, and the real header becomes data.
    """
    first = str(frame.columns[0]).strip()
    looks_like_comment = first.startswith(_COMMENT_PREFIXES)
    single_column = len(frame.columns) == 1

    if not (looks_like_comment or single_column):
        return

    reason = (
        f"the header row reads {first!r}, which looks like a comment"
        if looks_like_comment
        else f"the file parsed as a single column ({first!r})"
    )
    raise MappingError(
        f"Could not find a header row in {str(path)!r}: {reason}.\n"
        "If the file has comment or title lines above the header, skip them with "
        "--skip-rows N (skip_rows=N from Python). If the delimiter is unusual, "
        "set it with --sep."
    )


@dataclass
class Dataset:
    """A table plus the mapping that says how to submit it.

    Construct with :meth:`from_file` or :meth:`from_frame`; both validate the
    mapping against the data immediately, so a mistake surfaces before any
    upload is attempted.
    """

    frame: "pd.DataFrame"
    mapping: ColumnMapping
    name: Optional[str] = None
    #: Absolute path this dataset was read from, when it came from a file.
    source_path: Optional[str] = None
    #: Rows whose identifier came from the fallback column.
    gene_ids_filled: int = 0
    #: Rows left with no usable identifier at all.
    gene_ids_missing: int = 0

    @classmethod
    def from_file(
        cls,
        path: PathLike,
        mapping: ColumnMapping,
        sep: Optional[str] = None,
        name: Optional[str] = None,
        check_ranges: bool = True,
        skip_rows: int = 0,
        **read_csv_kwargs,
    ) -> "Dataset":
        """Load *path* and validate *mapping* against it.

        The dataset name defaults to the file stem, matching IPA's own habit of
        naming a dataset after the file it came from.

        Args:
            skip_rows: Lines of preamble above the header row to discard.
        """
        frame = load_table(path, sep=sep, skip_rows=skip_rows, **read_csv_kwargs)
        if name is None:
            name = os.path.splitext(os.path.basename(str(path)))[0]
        dataset = cls.from_frame(frame, mapping, name=name, check_ranges=check_ranges)
        dataset.source_path = os.path.abspath(str(path))
        return dataset

    @classmethod
    def from_frame(
        cls,
        frame: "pd.DataFrame",
        mapping: ColumnMapping,
        name: Optional[str] = None,
        check_ranges: bool = True,
    ) -> "Dataset":
        """Pair an in-memory DataFrame with *mapping* and validate it."""
        mapping.validate(frame, check_ranges=check_ranges)
        if len(frame) == 0:
            raise MappingError("Dataset contains no rows.")
        resolved, filled = mapping.resolve_gene_ids(frame)
        from .mapping import is_blank

        missing = int(resolved.map(is_blank).sum())
        if missing == len(frame):
            raise MappingError(
                f"Every row is missing an identifier in {mapping.gene_id_column!r}"
                + (
                    f" and {mapping.gene_id_fallback_column!r}"
                    if mapping.gene_id_fallback_column
                    else ""
                )
                + ". Check the column number and that the file has a header row."
            )
        return cls(
            frame=frame,
            mapping=mapping,
            name=name,
            gene_ids_filled=filled,
            gene_ids_missing=missing,
        )

    @property
    def measurement_warnings(self) -> list:
        """Warn where the data looks like a different measurement type than declared.

        Specifically: a genuine log ratio is centred on zero, so a real
        distribution always contains values between -1 and 1. Signed fold change
        -- the ``ratio`` if >= 1, else ``-1/ratio`` convention -- cannot contain
        any, by construction. A column declared ``logratio`` with nothing in that
        interval is therefore almost certainly fold change mislabelled, which
        inflates every magnitude exponentially while leaving directions intact.

        Warnings only. IPA is the authority, and an unusual but legitimate
        dataset should not be blocked.
        """
        import pandas as pd

        notes = []
        for obs in self.mapping.observations:
            for m in obs.measurements:
                if m.type is not MeasurementType.LOG_RATIO:
                    continue
                values = pd.to_numeric(self.frame[m.column], errors="coerce").dropna()
                # Too few points to say anything about the distribution.
                if len(values) < 50:
                    continue
                if ((values > -1) & (values < 1)).any():
                    continue
                notes.append(
                    f"column {m.column!r} is declared {MeasurementType.LOG_RATIO.value!r}, "
                    f"but none of its {len(values):,} values fall between -1 and 1. "
                    "A real log ratio is centred on zero and would have many; signed "
                    "fold change (ratio if >=1, else -1/ratio) can have none at all. "
                    "If these are fold changes, declare them 'foldchange' -- read as "
                    "log ratios they are interpreted as 2^value, inflating every "
                    "magnitude."
                )
        return notes

    @property
    def id_warnings(self) -> list:
        """Human-readable warnings about identifier coverage, empty if clean."""
        notes = []
        if self.gene_ids_filled:
            notes.append(
                f"{self.gene_ids_filled:,} of {len(self.frame):,} rows took their "
                f"identifier from the fallback column "
                f"{self.mapping.gene_id_fallback_column!r} "
                f"({self.mapping.gene_id_fallback_type}). IPA is told a single "
                f"gene ID type for the submission -- "
                f"{self.mapping.gene_id_type!r} -- so those rows are uploaded "
                "under that declaration and may not map."
            )
        if self.gene_ids_missing:
            notes.append(
                f"{self.gene_ids_missing:,} of {len(self.frame):,} rows have no "
                "usable identifier and will almost certainly be dropped by IPA."
            )
        return notes

    def __len__(self) -> int:
        return len(self.frame)

    @property
    def n_genes(self) -> int:
        """Number of identifier rows that will be uploaded."""
        return len(self.frame)

    def preview(self, rows: int = 5) -> "pd.DataFrame":
        """Return just the mapped columns, for eyeballing before upload."""
        return self.frame.loc[:, self.mapping.used_columns].head(rows)

    def describe(self) -> str:
        """Summarise the dataset and its mapping in one printable block."""
        head = (
            f"{self.name or 'dataset'}: {self.n_genes:,} "
            f"{'row' if self.n_genes == 1 else 'rows'}"
        )
        body = head + "\n" + self.mapping.describe()
        for note in self.warnings:
            body += f"\nWarning: {note}"
        return body

    # NOTE: submit() deliberately does not repeat these; the CLI prints
    # describe() for every dataset before uploading.

    @property
    def warnings(self) -> list:
        """Everything worth saying about this dataset before it is uploaded."""
        return self.measurement_warnings + self.id_warnings
