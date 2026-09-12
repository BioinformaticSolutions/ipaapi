"""Enumerations and value objects shared across the package."""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple

__all__ = ["MeasurementType", "AnalysisStatus", "ReferenceSet", "GENE_ID_TYPES"]

#: Every accepted ``geneidtype`` value, from the IPA Integration Module
#: documentation (April 2026), §3.1. Maps the wire value to the database it
#: refers to.
#:
#: Note that **species is carried by the identifier type**, not by a separate
#: parameter: ``hugo`` is human, ``mousesymeg`` mouse, ``ratsymeg`` rat. There
#: is no species argument in the API.
#:
#: Several entries are aliases for the same thing (``hugo`` / ``humansymeg`` /
#: ``humanegsym``). The list is not guessable from the interface -- the desktop
#: client's label "Gene Symbol - human (HUGO / HGNC, Entrez Gene)" corresponds
#: to ``hugo``, while ``genesymbol`` and ``hgnc`` are not accepted at all.
GENE_ID_TYPES = {
    "affymetrix": "Affymetrix",
    "affymetrixsnp": "Affymetrix SNP ID",
    "agilent": "Agilent",
    "abi": "Life Technologies (Applied Biosystems)",
    "life": "Life Technologies (Applied Biosystems)",
    "cas": "CAS Registry",
    "codelink": "CodeLink",
    "dbsnp": "dbSNP",
    "ensembl": "Ensembl",
    "entrezgene": "Entrez Gene",
    "locuslink": "Entrez Gene",
    "genbank": "GenBank",
    "genpept": "GenPept",
    "ginumber": "GI Number",
    "hugo": "Gene symbol -- human (Hugo / HGNC, Entrez Gene)",
    "humansymeg": "Gene symbol -- human (Hugo / HGNC, Entrez Gene)",
    "humanegsym": "Gene symbol -- human (Hugo / HGNC, Entrez Gene)",
    "mousesymeg": "Gene Symbol -- mouse (Entrez Gene)",
    "mouseegsym": "Gene Symbol -- mouse (Entrez Gene)",
    "ratsymeg": "Gene Symbol -- rat (Entrez Gene)",
    "rategsym": "Gene Symbol -- rat (Entrez Gene)",
    "hmdb": "Human Metabolome Database",
    "illumina": "Illumina",
    "ipi": "International Protein Index",
    "kegg": "KEGG ID",
    "mirbasemature": "miRBase (mature)",
    "mirbasestemloop": "miRBase (stemloop)",
    "pubchem": "PubChem CID",
    "refseq": "RefSeq",
    "ucsc_hg18": "UCSC isoform ids (hg18)",
    "ucsc_hg19": "UCSC isoform ids (hg19)",
    "swissprot": "UniProt/SwissProt Accession",
    "unigene": "UniGene",
}


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
        """Return whether *value* falls inside this type's accepted range.

        Infinities are rejected for every type. They used to pass: `inf >= 1`
        satisfies fold change, `logratio` and `other` are unbounded, and the
        upper bound of `ratio` and `intensity` is itself `inf`. IPA cannot read
        the text "Inf" as a number, and DESeq2 and edgeR emit it whenever a
        group has zero counts, so an ordinary differential expression table
        could clear this check and be scored on a fraction of its rows.
        """
        if value != value:  # NaN is always allowed; IPA treats it as missing.
            return True
        if value in (_INF, -_INF):
            return False
        if self in _DISCRETE:
            return float(value) in _DISCRETE[self]
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

#: Types IPA reads as a small set of codes rather than a continuous scale.
#: Checking these only against (-2, 2) let a continuous copy-number log ratio
#: through, which IPA then discards without comment.
_DISCRETE = {
    MeasurementType.GAIN_LOSS: frozenset({-2.0, -1.0, 0.0, 1.0, 2.0}),
    MeasurementType.CLASSIFICATION: frozenset({-2.0, -1.0, 0.0, 1.0, 2.0}),
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
    """Background set an analysis is scored against.

    Values per the IPA Integration Module documentation (April 2026), §4.1.3.

    .. important::
       **Omitting the parameter leaves the choice to IPA, and the rule is not
       reliable.** §4.1.3.1 says IPA picks by size when neither ``referenceset``
       nor ``referencesettype`` is given -- :attr:`IPKB` below 2000 identifiers,
       :attr:`DATASET` at 2000 or more.

       That has *not* been observed to hold: submissions of 1,804 to 6,245
       identifiers all came back scored against
       "Ingenuity Knowledge Base (Genes Only)". Since the behaviour is not
       predictable from the documentation, set this explicitly for any set of
       analyses you intend to compare against each other.

    Attributes:
        DATASET: The uploaded dataset is the background. Appropriate when the
            upload is a complete measured transcriptome.
        IPKB: The Ingenuity Knowledge Base -- "Genes Only" if the upload holds
            only genes, "Genes + Endogenous Chemicals" if chemicals are present.

    Array platforms (Affymetrix, Illumina and so on) may also be named, paired
    with a ``referencesettype``; those are not modelled here. See §4.1.3 of the
    documentation and the platform list it links to.
    """

    DATASET = "dataset"
    IPKB = "ipkb"
