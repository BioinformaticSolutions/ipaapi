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


#: Delimiters considered, in the order they are preferred on a tie.
_DELIMITERS = ("\t", ",", ";", "|")

#: How many lines past the header to read when deciding the delimiter.
_SNIFF_LINES = 8


def _sniff_separator(path: PathLike, default: str = "\t", skip_rows: int = 0) -> str:
    """Delimiter only. See :func:`_sniff` for the confidence flag."""
    return _sniff(path, default=default, skip_rows=skip_rows)[0]


def _sniff(path: PathLike, default: str = "\t", skip_rows: int = 0):
    """Guess the delimiter from the header AND the rows beneath it.

    This used to look at the header line alone, which is not enough evidence.
    A tab-separated file with a column named ``log2FC, shrunken`` sniffs as CSV,
    splits into two columns whose names contain literal tabs, and then passes
    every downstream guard: the header check sees two columns and no comment
    prefix, and the value column parses as entirely non-numeric. The CLI
    resolves ``--ID 0`` and ``--FC 1`` positionally onto the wreckage and IPA
    receives unmappable identifiers and no measurements, silently.

    A delimiter that is real splits the header and the data rows into the *same*
    number of fields. One that is an accident of punctuation inside a single
    cell does not. So each candidate is scored on agreement across several
    lines, and the header alone is only consulted if nothing agrees.

    *skip_rows* lines of preamble are stepped over first.
    """
    try:
        with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as fh:
            for _ in range(skip_rows):
                if fh.readline() == "":
                    raise MappingError(
                        f"{str(path)!r} has fewer than {skip_rows + 1} lines, so there "
                        "is no header row left after --skip-rows."
                    )
            lines = []
            # Count NON-blank lines against the budget. Counting every line
            # meant a file with nine blank lines above its header read as
            # empty. The hard cap keeps a file of nothing but blank lines from
            # being read to the end.
            for _ in range(_SNIFF_LINES * 20):
                line = fh.readline()
                if line == "":
                    break
                if line.strip():
                    lines.append(line)
                    if len(lines) >= _SNIFF_LINES:
                        break
    except OSError as exc:
        raise MappingError(f"Could not read {str(path)!r}: {exc}") from exc
    if not lines:
        raise MappingError(f"{str(path)!r} appears to be empty.")

    header, body = lines[0], lines[1:]

    best = None
    for candidate in _DELIMITERS:
        counts = [len(next(csv.reader([ln], delimiter=candidate))) for ln in lines]
        if counts[0] < 2:
            continue                       # does not split the header at all
        if len(set(counts)) != 1:
            continue                       # header and data disagree: not it
        # More real columns is better evidence; ties go to _DELIMITERS order.
        if best is None or counts[0] > best[1]:
            best = (candidate, counts[0])
    if best is not None:
        return best[0], True

    # Nothing agreed across lines -- a one-line file, ragged data, or a comment
    # line sitting where the header should be. Fall back to the old header-only
    # guess rather than refusing to read the file, but say it was a guess: the
    # header checks below treat an agreed delimiter as evidence and a guessed
    # one as no evidence at all.
    try:
        guess = csv.Sniffer().sniff(header, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        guess = default
    return guess, False


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
    agreed = False
    if sep is None:
        sep, agreed = _sniff(path, skip_rows=skip_rows)
    else:
        agreed = True          # the caller asserted it; trust them
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
        _warn_if_header_looks_wrong(path, frame, delimiter_agreed=agreed)
    return frame


#: Line prefixes that mark a comment in the formats these tables arrive in.
_COMMENT_PREFIXES = ("#", "//", ";", "!")


def _looks_like_field_names(columns) -> bool:
    """Whether these read as column headers rather than as a sentence.

    Agreement on the delimiter is not enough on its own: a prose comment like
    ``# DESeq2 results, liver, run 3`` above a three-column CSV splits into
    three fields and agrees, so it was being accepted as a header while the
    real header became data row 0. Field names are short and rarely contain
    more than a couple of words; a sentence fragment usually does.
    """
    names = [str(c).lstrip("#/;! ").strip() for c in columns]
    if any(not n for n in names):
        return False
    # Every field a single token. "DESeq2 results" is two words and so is a
    # plausible column name, so a word count is not a discriminator; requiring
    # one token is. It costs a genuine "#Gene ID" header, which then raises the
    # message below rather than being read -- the safe direction, since the
    # alternative is silently promoting the real header to data.
    return all(len(n.split()) == 1 and len(n) <= 40 for n in names)


def _warn_if_header_looks_wrong(
    path: PathLike, frame: "pd.DataFrame", delimiter_agreed: bool = False
) -> None:
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

    # A '#' in front of a real header row is a convention, not a comment --
    # bedtools, MACS and several aligners all write '#Chrom<TAB>Start'. The
    # discriminator is whether the delimiter was AGREED across the header and
    # the rows beneath it: a real header splits the whole file consistently,
    # while a prose comment only splits because it happens to contain a comma.
    # Without that agreement this stays an error, because the old advice is
    # destructive here -- --skip-rows 1 promotes the first DATA row to header.
    if (
        looks_like_comment
        and not single_column
        and delimiter_agreed
        and _looks_like_field_names(frame.columns)
    ):
        stripped = str(frame.columns[0]).lstrip("#/;! ").strip()
        rest = [str(c) for c in frame.columns[1:]]
        # Never manufacture an empty name or a collision that is not in the
        # file: "#" alone as the first column, or "#Gene ... Gene", would
        # otherwise surface as a complaint about duplicates the user does not
        # have.
        if stripped and stripped not in rest:
            frame.columns = [stripped] + rest
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
        "set it with --sep.\n"
        "If that line IS the header and simply starts with a marker character, "
        "it is read as one automatically when every field is a single word; "
        "remove the marker, or the spaces inside the field names, and it will "
        "be picked up."
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

        notes.extend(self._false_discovery_scale_warnings())
        return notes

    def _false_discovery_scale_warnings(self) -> list:
        """Warn when an FDR column looks like a fraction rather than a percent.

        IPA reads ``falsediscovery`` as a **percentage** in [0, 100]. Statistical
        software almost universally emits q-values as fractions in [0, 1]. Both
        are inside the accepted range, so nothing is rejected and nothing is
        discarded -- a q-value of 0.05 is simply taken as 0.05%, a threshold two
        orders of magnitude stricter than intended, and any cutoff applied in
        IPA silently keeps far less than expected.

        This is the one measurement type where the range check cannot help,
        because the wrong scale is a valid value. Hence a warning on the shape
        of the distribution instead.
        """
        import pandas as pd

        notes = []
        for obs in self.mapping.observations:
            for m in obs.measurements:
                if m.type is not MeasurementType.FALSE_DISCOVERY:
                    continue
                values = pd.to_numeric(self.frame[m.column], errors="coerce").dropna()
                if values.empty or (values > 1).any():
                    continue  # already on a percentage scale, or nothing to judge
                notes.append(
                    f"column {m.column!r} is declared "
                    f"{MeasurementType.FALSE_DISCOVERY.value!r} and every one of its "
                    f"{len(values):,} values is <= 1. IPA reads this type as a "
                    "PERCENTAGE in [0, 100], so 0.05 means 0.05%, not 5%. If these "
                    "are ordinary q-values, multiply the column by 100 before "
                    "submitting -- IPA accepts them either way and cannot tell the "
                    "difference, so nothing will be rejected."
                )
                if m.cutoff is not None and m.cutoff <= 1:
                    notes[-1] += (
                        f"\n    The cutoff on this column is {m.cutoff:g}, which is on "
                        "that same percentage scale and is NOT converted for you. If "
                        "you scale the column, scale the cutoff with it -- a column "
                        "multiplied by 100 against an unchanged cutoff filters 100x "
                        "more strictly than intended, and just as silently."
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
