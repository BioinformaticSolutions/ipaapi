"""Loading tabular datasets and pairing them with a :class:`ColumnMapping`."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union

from .errors import MappingError
from .mapping import ColumnMapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

__all__ = ["Dataset", "load_table"]

PathLike = Union[str, "os.PathLike[str]"]


def _sniff_separator(path: PathLike, default: str = "\t") -> str:
    """Guess the delimiter of a delimited text file from its first line."""
    try:
        with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as fh:
            sample = fh.readline()
    except OSError as exc:
        raise MappingError(f"Could not read {path!r}: {exc}") from exc
    if not sample:
        raise MappingError(f"{path!r} appears to be empty.")
    try:
        return csv.Sniffer().sniff(sample, delimiters="\t,;|").delimiter
    except csv.Error:
        return default


def load_table(path: PathLike, sep: Optional[str] = None, **read_csv_kwargs) -> "pd.DataFrame":
    """Read a delimited text file into a DataFrame with a real header row.

    Unlike the original demo, which read the file with ``header=None`` and then
    treated row 0 as text, this keeps the header as column names so a
    :class:`ColumnMapping` can address columns by name.

    Args:
        path: File to read.
        sep: Field delimiter. Sniffed from the first line when omitted.
        **read_csv_kwargs: Passed through to :func:`pandas.read_csv`.
    """
    import pandas as pd

    if sep is None:
        sep = _sniff_separator(path)
    read_csv_kwargs.setdefault("dtype", object)
    read_csv_kwargs.setdefault("encoding", "utf-8-sig")
    try:
        frame = pd.read_csv(path, sep=sep, **read_csv_kwargs)
    except Exception as exc:  # pandas raises a wide variety of parse errors
        raise MappingError(f"Could not parse {path!r} as a table: {exc}") from exc
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame


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

    @classmethod
    def from_file(
        cls,
        path: PathLike,
        mapping: ColumnMapping,
        sep: Optional[str] = None,
        name: Optional[str] = None,
        check_ranges: bool = True,
        **read_csv_kwargs,
    ) -> "Dataset":
        """Load *path* and validate *mapping* against it.

        The dataset name defaults to the file stem, matching IPA's own habit of
        naming a dataset after the file it came from.
        """
        frame = load_table(path, sep=sep, **read_csv_kwargs)
        if name is None:
            name = os.path.splitext(os.path.basename(str(path)))[0]
        return cls.from_frame(frame, mapping, name=name, check_ranges=check_ranges)

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
        return cls(frame=frame, mapping=mapping, name=name)

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
        head = f"{self.name or 'dataset'}: {self.n_genes} rows"
        return head + "\n" + self.mapping.describe()
