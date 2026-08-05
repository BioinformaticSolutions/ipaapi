"""Build the ``multiobsanalysis`` request body.

IPA's ``/pa/api/v2/multiobsanalysis`` endpoint takes a single
``application/x-www-form-urlencoded`` body that carries both the analysis
settings and the entire dataset, as a long run of repeated parameters.

The parameter naming is positional and slightly irregular, so it is worth
writing down. For measurement slot ``k`` (zero-based) and observation ``i``
(zero-based):

============================  ==============================================
Parameter                     Meaning
============================  ==============================================
``expvaltype`` / ``expvaltypeK+1``   measurement type for slot k (global)
``cutoff`` / ``cutoffK+1``           cutoff for slot k (global, optional)
``obsI+1name``                       observation name
``expvalname`` / ``expvalK+1name``   column label, slot k, first observation
``obsI+1expvalname``                 column label, slot k=0, later observations
``obsI+1expvalK+1name``              column label, slot k>0, later observations
``geneid``                           one per data row
``expvalue`` / ``expvalK+1``         one per slot per observation, per row
============================  ==============================================

Note that the per-row value parameters carry no observation prefix: they simply
repeat, cycling through the slots of observation 1, then observation 2, and so
on. Order is therefore load-bearing, which is why this module builds an explicit
ordered list of pairs.

The original demo concatenated these into a string by hand with no
percent-encoding, so any gene identifier, column header or observation name
containing ``&``, ``=``, ``+``, ``%`` or a space silently corrupted the request.
Here the pairs are handed to :func:`urllib.parse.urlencode`, which encodes them
correctly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

from .mapping import ColumnMapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

__all__ = ["build_submission_pairs", "encode_submission"]

Pair = Tuple[str, str]

_MISSING = "NaN"


def _slot_key(base: str, index: int) -> str:
    """``expvaltype``, ``expvaltype2``, ``expvaltype3`` ... for slot *index*."""
    return base if index == 0 else f"{base}{index + 1}"


def _column_name_key(obs_index: int, slot_index: int) -> str:
    """Parameter naming the source column for (*obs_index*, *slot_index*)."""
    prefix = "" if obs_index == 0 else f"obs{obs_index + 1}"
    if slot_index == 0:
        return f"{prefix}expvalname"
    return f"{prefix}expval{slot_index + 1}name"


def _value_key(slot_index: int) -> str:
    """Parameter carrying a data value in slot *slot_index*."""
    return "expvalue" if slot_index == 0 else f"expval{slot_index + 1}"


def _format(value) -> str:
    """Render a cell as IPA expects, mapping missing values to ``NaN``."""
    if value is None:
        return _MISSING
    if isinstance(value, float) and value != value:
        return _MISSING
    text = str(value).strip()
    if text == "" or text.lower() in {"na", "nan", "none", "null"}:
        return _MISSING
    return text


def build_submission_pairs(
    frame: "pd.DataFrame",
    mapping: ColumnMapping,
    application_name: str,
    project_name: str,
    dataset_name: str,
    analysis_name: Optional[str] = None,
    reference_set: str = "dataset",
    ipa_view: str = "none",
) -> List[Pair]:
    """Return the full ordered parameter list for one submission.

    Kept separate from :func:`encode_submission` so tests can assert on the
    structure without decoding a URL-encoded blob.
    """
    import pandas as pd

    types = mapping.measurement_types
    pairs: List[Pair] = [
        ("applicationname", application_name),
        ("projectname", project_name),
        ("ipaview", ipa_view),
        ("datasetname", dataset_name),
        ("analysisname", analysis_name or dataset_name),
        ("referenceset", reference_set),
        ("geneidtype", mapping.gene_id_type),
        ("genecolname", mapping.gene_id_label or mapping.gene_id_column),
    ]

    for i, obs in enumerate(mapping.observations):
        pairs.append((f"obs{i + 1}name", obs.name))

    for k, mtype in enumerate(types):
        pairs.append((_slot_key("expvaltype", k), mtype.value))

    for i, obs in enumerate(mapping.observations):
        for k, measurement in enumerate(mapping.ordered_measurements(obs)):
            pairs.append((_column_name_key(i, k), measurement.display_name))

    for k, cutoff in enumerate(mapping.cutoffs):
        if cutoff is not None:
            pairs.append((_slot_key("cutoff", k), f"{cutoff:g}"))

    # Identifiers may be coalesced from a fallback column; values are taken in
    # canonical submission order. itertuples keeps this workable on large files.
    gene_ids, _ = mapping.resolve_gene_ids(frame)
    values = frame.loc[:, mapping.value_columns]
    n_slots = len(types)

    for gene_id, row in zip(gene_ids.tolist(), values.itertuples(index=False, name=None)):
        pairs.append(("geneid", _format(gene_id)))
        for offset, value in enumerate(row):
            pairs.append((_value_key(offset % n_slots), _format(value)))

    return pairs


def encode_submission(pairs: List[Pair]) -> str:
    """Percent-encode *pairs* as an ``x-www-form-urlencoded`` body."""
    return urlencode(pairs)
