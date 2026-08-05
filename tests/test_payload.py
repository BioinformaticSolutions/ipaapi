"""The submission body is positional and fiddly; these tests pin its shape."""

from urllib.parse import parse_qsl

import pandas as pd
import pytest

from ipaapi import ColumnMapping, Measurement, MeasurementType, Observation
from ipaapi._payload import build_submission_pairs, encode_submission


def build(frame, mapping, **kwargs):
    kwargs.setdefault("application_name", "PythonAPI")
    kwargs.setdefault("project_name", "Proj")
    kwargs.setdefault("dataset_name", "DS")
    return build_submission_pairs(frame=frame, mapping=mapping, **kwargs)


def keys_named(pairs, name):
    return [v for k, v in pairs if k == name]


def test_header_parameters(frame, two_obs_mapping):
    pairs = build(frame, two_obs_mapping)
    head = dict(pairs[:8])
    assert head["applicationname"] == "PythonAPI"
    assert head["projectname"] == "Proj"
    assert head["datasetname"] == "DS"
    assert head["analysisname"] == "DS"  # defaults to dataset name
    assert head["geneidtype"] == "ensembl"
    assert head["genecolname"] == "Gene IDs"
    assert head["referenceset"] == "dataset"


def test_observation_names_are_one_indexed(frame, two_obs_mapping):
    pairs = build(frame, two_obs_mapping)
    assert keys_named(pairs, "obs1name") == ["treated vs ctrl"]
    assert keys_named(pairs, "obs2name") == ["control vs ctrl"]


def test_measurement_types_declared_once_globally(frame, two_obs_mapping):
    pairs = build(frame, two_obs_mapping)
    assert keys_named(pairs, "expvaltype") == ["foldchange"]
    assert keys_named(pairs, "expvaltype2") == ["pvalue"]
    # Not declared per observation.
    assert keys_named(pairs, "obs2expvaltype") == []


def test_column_labels_use_the_irregular_naming_scheme(frame, two_obs_mapping):
    pairs = build(frame, two_obs_mapping)
    # First observation: no obs prefix.
    assert keys_named(pairs, "expvalname") == ["FC treated"]
    assert keys_named(pairs, "expval2name") == ["p treated"]
    # Later observations: obsN prefix, and reordered into canonical slot order.
    assert keys_named(pairs, "obs2expvalname") == ["FC control"]
    assert keys_named(pairs, "obs2expval2name") == ["p control"]


def test_cutoffs_are_global_and_skip_none(frame, two_obs_mapping):
    pairs = build(frame, two_obs_mapping)
    assert keys_named(pairs, "cutoff") == ["1.5"]
    assert keys_named(pairs, "cutoff2") == []  # p-value had no cutoff


def test_row_values_cycle_through_slots_per_observation(frame, two_obs_mapping):
    pairs = build(frame, two_obs_mapping)
    rows = [p for p in pairs if p[0] in ("geneid", "expvalue", "expval2")]
    # 3 genes x (1 id + 2 observations x 2 slots) = 15 pairs
    assert len(rows) == 15
    first_row = rows[:5]
    assert first_row == [
        ("geneid", "ENSG1"),
        ("expvalue", "2.0"),   # FC treated
        ("expval2", "0.01"),   # p treated
        ("expvalue", "1.2"),   # FC control
        ("expval2", "0.05"),   # p control
    ]


def test_missing_values_become_nan():
    frame = pd.DataFrame({"id": ["g1", "g2"], "fc": [None, 2.0], "p": ["", 0.5]})
    mapping = ColumnMapping(
        gene_id_column="id",
        gene_id_type="ensembl",
        observations=[
            Observation(
                "a",
                [
                    Measurement("fc", MeasurementType.FOLD_CHANGE),
                    Measurement("p", MeasurementType.P_VALUE),
                ],
            )
        ],
    )
    pairs = build(frame, mapping)
    assert keys_named(pairs, "expvalue") == ["NaN", "2.0"]
    assert keys_named(pairs, "expval2") == ["NaN", "0.5"]


def test_labels_override_column_names(frame):
    mapping = ColumnMapping(
        gene_id_column="Gene IDs",
        gene_id_type="ensembl",
        gene_id_label="Ensembl ID",
        observations=[
            Observation(
                "a",
                [Measurement("FC treated", MeasurementType.FOLD_CHANGE, label="log2FC")],
            )
        ],
    )
    pairs = build(frame, mapping)
    assert dict(pairs[:8])["genecolname"] == "Ensembl ID"
    assert keys_named(pairs, "expvalname") == ["log2FC"]


def test_encoding_escapes_hostile_characters():
    """The original demo concatenated this by hand and corrupted such values."""
    frame = pd.DataFrame({"id": ["gene&weird=1"], "fc": [2.0]})
    mapping = ColumnMapping(
        gene_id_column="id",
        gene_id_type="ensembl",
        observations=[
            Observation(
                "drug A & drug B",
                [Measurement("fc", MeasurementType.FOLD_CHANGE, label="Fold Change")],
            )
        ],
    )
    body = encode_submission(build(frame, mapping))
    assert "gene&weird=1" not in body  # raw form would have broken parsing
    round_tripped = dict(parse_qsl(body))
    assert round_tripped["geneid"] == "gene&weird=1"
    assert round_tripped["obs1name"] == "drug A & drug B"
    assert round_tripped["expvalname"] == "Fold Change"


def test_analysis_name_can_differ_from_dataset_name(frame, two_obs_mapping):
    pairs = build(frame, two_obs_mapping, analysis_name="Custom run")
    assert dict(pairs[:8])["analysisname"] == "Custom run"
