"""Enumerations and value objects shared across the package."""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple

__all__ = ["MeasurementType", "AnalysisStatus", "ReferenceSet"]


class MeasurementType(str, Enum):
    """A measurement type accepted by IPA for an expression value column.

    The wire value is what IPA expects in the ``expvaltype`` parameters. Each
    member also knows the value range IPA considers valid, which the package
    uses to sanity-check data before upload.
    """

    RATIO = "ratio"
    FOLD_CHANGE = "foldchange"
    LOG_RATIO = "logratio"
    P_VALUE = "pvalue"
    FALSE_DISCOVERY = "falsediscovery"
    INTENSITY = "intensity"
    OTHER = "other"
    GAIN_LOSS = "gain_loss"
    CLASSIFICATION = "classification"

    @property
    def label(self) -> str:
        """Human-readable name, as shown in the IPA interface."""
        return _LABELS[self]

    @property
    def valid_range(self) -> Optional[Tuple[float, float]]:
        """Inclusive ``(low, high)`` bounds, or ``None`` if unbounded.

        Note that :attr:`FOLD_CHANGE` is discontinuous -- IPA accepts
        ``(-inf, -1]`` and ``[1, +inf)`` but not the open interval between --
        so its bounds are reported as ``None`` and checked separately by
        :meth:`is_plausible`.
        """
        return _RANGES[self]

    def is_plausible(self, value: float) -> bool:
        """Return whether *value* falls inside this type's accepted range."""
        if value != value:  # NaN is always allowed; IPA treats it as missing.
            return True
        if self is MeasurementType.FOLD_CHANGE:
            return value >= 1.0 or value <= -1.0
        bounds = self.valid_range
        if bounds is None:
            return True
        low, high = bounds
        return low <= value <= high


_LABELS = {
    MeasurementType.RATIO: "Ratio",
    MeasurementType.FOLD_CHANGE: "Fold Change",
    MeasurementType.LOG_RATIO: "Log Ratio",
    MeasurementType.P_VALUE: "p-value",
    MeasurementType.FALSE_DISCOVERY: "False Discovery Rate (q-value)",
    MeasurementType.INTENSITY: "Intensity",
    MeasurementType.OTHER: "Other (normalized around zero)",
    MeasurementType.GAIN_LOSS: "Variant Gain/Loss",
    MeasurementType.CLASSIFICATION: "Variant ACMG Classification",
}

_INF = float("inf")
_RANGES = {
    MeasurementType.RATIO: (0.0, _INF),
    MeasurementType.FOLD_CHANGE: None,  # discontinuous; see is_plausible()
    MeasurementType.LOG_RATIO: None,
    MeasurementType.P_VALUE: (0.0, 1.0),
    MeasurementType.FALSE_DISCOVERY: (0.0, 100.0),
    MeasurementType.INTENSITY: (0.0, _INF),
    MeasurementType.OTHER: None,
    MeasurementType.GAIN_LOSS: (-2.0, 2.0),
    MeasurementType.CLASSIFICATION: (-2.0, 2.0),
}


class AnalysisStatus(str, Enum):
    """Terminal and non-terminal states reported by ``/analysisstatus``.

    Only codes ``3``, ``4`` and ``5`` are documented by the API as terminal.
    Anything else is reported as :attr:`IN_PROGRESS`, which is why the raw code
    is preserved on :attr:`code`.
    """

    IN_PROGRESS = "in_progress"
    SUCCEEDED = "3"
    FAILED = "4"
    CANCELED = "5"

    @classmethod
    def from_code(cls, code: str) -> "AnalysisStatus":
        """Map a raw status code from the API onto a member."""
        code = (code or "").strip()
        for member in (cls.SUCCEEDED, cls.FAILED, cls.CANCELED):
            if code == member.value:
                return member
        return cls.IN_PROGRESS

    @property
    def is_terminal(self) -> bool:
        """Whether the analysis has stopped running, successfully or not."""
        return self is not AnalysisStatus.IN_PROGRESS

    @property
    def succeeded(self) -> bool:
        return self is AnalysisStatus.SUCCEEDED


class ReferenceSet(str, Enum):
    """Background gene set an analysis is scored against.

    Only ``dataset`` is known to be accepted -- it is the sole value QIAGEN's
    demo code ever sent, and the API's accepted vocabulary is not documented.
    A previously guessed ``ingenuity`` value was rejected by the server with a
    generic HTML error page, so it has been removed rather than left to mislead.

    To score against the Ingenuity Knowledge Base instead, omit the parameter
    (pass ``reference_set=None``) so IPA applies whatever default it considers
    correct.

    .. note::
       ``dataset`` uses the uploaded genes as background, which is right for a
       complete measured transcriptome. For a **pre-filtered** hit list the
       background and the analysis-ready set are the same, which degenerates
       the enrichment statistics: z-scores are still produced, but overlap
       p-values are not meaningful.
    """

    DATASET = "dataset"
