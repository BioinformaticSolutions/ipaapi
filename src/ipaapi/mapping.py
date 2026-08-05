"""Describe how the columns of a dataset map onto an IPA analysis.

The demo code this package grew out of assumed a very rigid file layout: the
gene identifier in column 0, followed by ``n_observations x n_measurements``
value columns in strict repeating order, with every observation carrying the
same measurement types in the same positions. Real files rarely look like that.

:class:`ColumnMapping` replaces that assumption with an explicit declaration.
You name the gene identifier column and describe each observation as a set of
``(column, measurement type)`` pairs. Columns may appear in any order, be named
anything, and be interleaved with columns the analysis should ignore.

One constraint is genuinely imposed by the IPA API and cannot be designed away:
the *sequence of measurement types* is global to the submission (the wire format
declares ``expvaltype``, ``expvaltype2``, ... once for the whole request, then
supplies per-observation column names against those slots). So every observation
must contribute exactly one column per declared measurement type. Cutoffs are
likewise global per measurement slot. Both rules are enforced here, before
anything is sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, List, Optional, Sequence, Tuple, Union

from .errors import MappingError
from .models import MeasurementType

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

__all__ = ["Measurement", "Observation", "ColumnMapping"]

MeasurementLike = Union[MeasurementType, str]


def _coerce_type(value: MeasurementLike) -> MeasurementType:
    if isinstance(value, MeasurementType):
        return value
    try:
        return MeasurementType(str(value).strip().lower())
    except ValueError:
        allowed = ", ".join(m.value for m in MeasurementType)
        raise MappingError(
            f"Unknown measurement type {value!r}. Allowed types: {allowed}."
        ) from None


@dataclass(frozen=True)
class Measurement:
    """One value column within an observation.

    Args:
        column: Column header as it appears in the dataset.
        type: Measurement type for the column.
        cutoff: Optional significance cutoff. Because IPA applies cutoffs per
            measurement slot rather than per observation, every observation that
            declares this measurement type must give the same cutoff.
        label: Name shown for the column inside IPA. Defaults to ``column``.
    """

    column: str
    type: MeasurementType
    cutoff: Optional[float] = None
    label: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", _coerce_type(self.type))
        if not str(self.column).strip():
            raise MappingError("Measurement.column must be a non-empty column name.")

    @property
    def display_name(self) -> str:
        """Name IPA should show for this column."""
        return self.label if self.label else self.column


@dataclass
class Observation:
    """A named sample or contrast, and the value columns belonging to it.

    Args:
        name: Observation name as it should appear in IPA.
        measurements: The value columns for this observation, in any order.
    """

    name: str
    measurements: List[Measurement] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise MappingError("Observation.name must be non-empty.")
        coerced: List[Measurement] = []
        for m in self.measurements:
            if isinstance(m, Measurement):
                coerced.append(m)
            elif isinstance(m, dict):
                coerced.append(Measurement(**m))
            elif isinstance(m, (tuple, list)) and len(m) >= 2:
                coerced.append(Measurement(*m))
            else:
                raise MappingError(
                    f"Cannot interpret {m!r} as a Measurement in observation {self.name!r}."
                )
        self.measurements = coerced
        if not self.measurements:
            raise MappingError(f"Observation {self.name!r} has no measurement columns.")
        seen = set()
        for m in self.measurements:
            if m.type in seen:
                raise MappingError(
                    f"Observation {self.name!r} declares measurement type "
                    f"{m.type.value!r} more than once. Each observation may supply "
                    "at most one column per measurement type."
                )
            seen.add(m.type)

    def by_type(self, mtype: MeasurementType) -> Measurement:
        """Return this observation's column for *mtype*."""
        for m in self.measurements:
            if m.type is mtype:
                return m
        raise MappingError(
            f"Observation {self.name!r} has no column of type {mtype.value!r}."
        )

    @property
    def columns(self) -> List[str]:
        return [m.column for m in self.measurements]


@dataclass
class ColumnMapping:
    """A complete description of how a dataset maps onto an IPA submission.

    Args:
        gene_id_column: Header of the column holding gene identifiers.
        gene_id_type: IPA gene identifier type, e.g. ``"ensembl"``,
            ``"entrezgene"``, ``"genesymbol"``, ``"affymetrix"``.
        observations: One :class:`Observation` per sample or contrast.
        gene_id_label: Name shown for the identifier column in IPA. Defaults to
            ``gene_id_column``.

    Example:
        >>> mapping = ColumnMapping(
        ...     gene_id_column="ID",
        ...     gene_id_type="ensembl",
        ...     observations=[
        ...         Observation("Gemfib vs ctrl", [
        ...             Measurement("FC", MeasurementType.FOLD_CHANGE, cutoff=1.5),
        ...             Measurement("pval", MeasurementType.P_VALUE),
        ...         ]),
        ...     ],
        ... )
    """

    gene_id_column: str
    gene_id_type: str
    observations: List[Observation] = field(default_factory=list)
    gene_id_label: Optional[str] = None

    def __post_init__(self) -> None:
        if not str(self.gene_id_column).strip():
            raise MappingError("gene_id_column must be a non-empty column name.")
        if not str(self.gene_id_type).strip():
            raise MappingError("gene_id_type must be set, e.g. 'ensembl'.")
        coerced: List[Observation] = []
        for obs in self.observations:
            if isinstance(obs, Observation):
                coerced.append(obs)
            elif isinstance(obs, dict):
                coerced.append(Observation(**obs))
            else:
                raise MappingError(f"Cannot interpret {obs!r} as an Observation.")
        self.observations = coerced
        if not self.observations:
            raise MappingError("A ColumnMapping needs at least one observation.")

        names = [o.name for o in self.observations]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise MappingError(
                "Observation names must be unique; repeated: "
                + ", ".join(sorted(duplicates))
            )

        self._check_consistent_types()
        self._check_consistent_cutoffs()

    # -- invariants imposed by the IPA wire format -------------------------

    def _check_consistent_types(self) -> None:
        reference = set(self.measurement_types)
        for obs in self.observations[1:]:
            got = set(m.type for m in obs.measurements)
            if got != reference:
                missing = sorted(t.value for t in reference - got)
                extra = sorted(t.value for t in got - reference)
                detail = []
                if missing:
                    detail.append("missing " + ", ".join(missing))
                if extra:
                    detail.append("unexpected " + ", ".join(extra))
                raise MappingError(
                    f"Observation {obs.name!r} declares a different set of measurement "
                    f"types than {self.observations[0].name!r} ({'; '.join(detail)}). "
                    "IPA declares measurement types once for the whole submission, so "
                    "every observation must supply exactly one column per type."
                )

    def _check_consistent_cutoffs(self) -> None:
        for mtype in self.measurement_types:
            values = {}
            for obs in self.observations:
                values[obs.name] = obs.by_type(mtype).cutoff
            distinct = set(values.values())
            if len(distinct) > 1:
                detail = ", ".join(f"{k}={v!r}" for k, v in values.items())
                raise MappingError(
                    f"Conflicting cutoffs for measurement type {mtype.value!r}: {detail}. "
                    "IPA applies one cutoff per measurement type across the whole "
                    "submission, so it cannot vary between observations."
                )

    # -- derived views -----------------------------------------------------

    @property
    def measurement_types(self) -> List[MeasurementType]:
        """Canonical measurement slot order, taken from the first observation."""
        return [m.type for m in self.observations[0].measurements]

    @property
    def cutoffs(self) -> List[Optional[float]]:
        """Cutoff per measurement slot, in canonical order."""
        first = self.observations[0]
        return [first.by_type(t).cutoff for t in self.measurement_types]

    def ordered_measurements(self, obs: Observation) -> List[Measurement]:
        """Return *obs*'s measurements re-ordered into canonical slot order."""
        return [obs.by_type(t) for t in self.measurement_types]

    @property
    def value_columns(self) -> List[str]:
        """Every value column used, in canonical submission order."""
        out: List[str] = []
        for obs in self.observations:
            out.extend(m.column for m in self.ordered_measurements(obs))
        return out

    @property
    def used_columns(self) -> List[str]:
        """The identifier column plus every value column."""
        return [self.gene_id_column] + self.value_columns

    # -- validation against real data --------------------------------------

    def validate(self, frame: "pd.DataFrame", check_ranges: bool = True) -> None:
        """Check this mapping against *frame*, raising :class:`MappingError`.

        Verifies that every declared column exists, that no column is claimed
        twice, and (when *check_ranges*) that the values in each column fall
        inside the range IPA accepts for its measurement type.
        """
        available = list(frame.columns)
        missing = [c for c in self.used_columns if c not in available]
        if missing:
            raise MappingError(
                "Columns declared in the mapping are not present in the dataset: "
                + ", ".join(repr(c) for c in missing)
                + ". Available columns: "
                + ", ".join(repr(c) for c in available[:25])
                + (" ..." if len(available) > 25 else "")
            )

        used = self.used_columns
        repeated = sorted({c for c in used if used.count(c) > 1})
        if repeated:
            raise MappingError(
                "The same column is claimed more than once by the mapping: "
                + ", ".join(repr(c) for c in repeated)
            )

        if check_ranges:
            self._check_ranges(frame)

    def _check_ranges(self, frame: "pd.DataFrame") -> None:
        import pandas as pd

        problems: List[str] = []
        for obs in self.observations:
            for m in obs.measurements:
                series = pd.to_numeric(frame[m.column], errors="coerce").dropna()
                if series.empty:
                    continue
                bad = [v for v in series.tolist() if not m.type.is_plausible(float(v))]
                if bad:
                    sample = ", ".join(f"{v:g}" for v in bad[:3])
                    problems.append(
                        f"column {m.column!r} (observation {obs.name!r}) is declared "
                        f"{m.type.value!r} but holds {len(bad)} out-of-range value(s), "
                        f"e.g. {sample}"
                    )
        if problems:
            raise MappingError(
                "Values do not match their declared measurement types:\n  - "
                + "\n  - ".join(problems)
                + "\nPass check_ranges=False to submit anyway."
            )

    # -- convenience constructors ------------------------------------------

    @classmethod
    def from_blocks(
        cls,
        columns: Sequence[str],
        gene_id_type: str,
        observation_names: Sequence[str],
        measurement_types: Iterable[MeasurementLike],
        cutoffs: Optional[Sequence[Optional[float]]] = None,
        gene_id_column: Optional[str] = None,
    ) -> "ColumnMapping":
        """Build a mapping from the rigid layout the original demo assumed.

        Treats *columns* as the identifier column followed by contiguous blocks
        of measurements, one block per observation, each block in
        *measurement_types* order. Useful for files that really are laid out
        that way, and as a migration path from the old ``ipa_analyze`` call.
        """
        columns = list(columns)
        types = [_coerce_type(t) for t in measurement_types]
        gene_col = gene_id_column if gene_id_column is not None else columns[0]
        value_cols = [c for c in columns if c != gene_col]

        expected = len(observation_names) * len(types)
        if len(value_cols) < expected:
            raise MappingError(
                f"Expected at least {expected} value columns for "
                f"{len(observation_names)} observation(s) x {len(types)} measurement(s), "
                f"but found {len(value_cols)}."
            )

        cut = list(cutoffs) if cutoffs is not None else [None] * len(types)
        if len(cut) != len(types):
            raise MappingError(
                f"Got {len(cut)} cutoff(s) for {len(types)} measurement type(s); "
                "supply one per type, using None where there is no cutoff."
            )

        observations = []
        for i, obs_name in enumerate(observation_names):
            block = value_cols[i * len(types) : (i + 1) * len(types)]
            observations.append(
                Observation(
                    name=obs_name,
                    measurements=[
                        Measurement(column=col, type=t, cutoff=c)
                        for col, t, c in zip(block, types, cut)
                    ],
                )
            )
        return cls(
            gene_id_column=gene_col,
            gene_id_type=gene_id_type,
            observations=observations,
        )

    def describe(self) -> str:
        """Return a human-readable summary, handy for logging before upload."""
        lines = [
            f"gene id: {self.gene_id_column!r} ({self.gene_id_type})",
            f"observations: {len(self.observations)}",
        ]
        for obs in self.observations:
            lines.append(f"  {obs.name}:")
            for m in self.ordered_measurements(obs):
                cut = "" if m.cutoff is None else f", cutoff {m.cutoff:g}"
                lines.append(f"    {m.column!r} -> {m.type.label}{cut}")
        return "\n".join(lines)
