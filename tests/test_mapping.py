"""Column mapping: flexibility where the API allows it, hard errors where it does not."""

import pandas as pd
import pytest

from ipaapi import ColumnMapping, Dataset, Measurement, MeasurementType, Observation
from ipaapi.errors import MappingError


def test_columns_may_be_in_any_order(frame, two_obs_mapping):
    two_obs_mapping.validate(frame)
    # Canonical order is taken from the first observation and applied to all.
    assert two_obs_mapping.measurement_types == [
        MeasurementType.FOLD_CHANGE,
        MeasurementType.P_VALUE,
    ]
    second = two_obs_mapping.observations[1]
    assert [m.column for m in two_obs_mapping.ordered_measurements(second)] == [
        "FC control",
        "p control",
    ]


def test_unmapped_columns_are_ignored(frame, two_obs_mapping):
    assert "notes" not in two_obs_mapping.used_columns
    two_obs_mapping.validate(frame)


def test_measurement_type_accepts_strings():
    obs = Observation("o", [Measurement("c", "foldchange")])
    assert obs.measurements[0].type is MeasurementType.FOLD_CHANGE


def test_unknown_measurement_type_rejected():
    with pytest.raises(MappingError, match="Unknown measurement type"):
        Measurement("c", "not_a_real_type")


def test_missing_column_rejected(frame, two_obs_mapping):
    two_obs_mapping.observations[0].measurements[0] = Measurement(
        "does not exist", MeasurementType.FOLD_CHANGE, cutoff=1.5
    )
    with pytest.raises(MappingError, match="not present in the dataset"):
        two_obs_mapping.validate(frame)


def test_inconsistent_measurement_types_rejected():
    with pytest.raises(MappingError, match="different set of measurement types"):
        ColumnMapping(
            gene_id_column="id",
            gene_id_type="ensembl",
            observations=[
                Observation("a", [Measurement("fc_a", MeasurementType.FOLD_CHANGE)]),
                Observation("b", [Measurement("p_b", MeasurementType.P_VALUE)]),
            ],
        )


def test_conflicting_cutoffs_rejected():
    with pytest.raises(MappingError, match="Conflicting cutoffs"):
        ColumnMapping(
            gene_id_column="id",
            gene_id_type="ensembl",
            observations=[
                Observation(
                    "a", [Measurement("fc_a", MeasurementType.FOLD_CHANGE, cutoff=1.5)]
                ),
                Observation(
                    "b", [Measurement("fc_b", MeasurementType.FOLD_CHANGE, cutoff=2.0)]
                ),
            ],
        )


def test_duplicate_measurement_type_within_observation_rejected():
    with pytest.raises(MappingError, match="more than once"):
        Observation(
            "a",
            [
                Measurement("p1", MeasurementType.P_VALUE),
                Measurement("p2", MeasurementType.P_VALUE),
            ],
        )


def test_duplicate_observation_names_rejected():
    with pytest.raises(MappingError, match="unique"):
        ColumnMapping(
            gene_id_column="id",
            gene_id_type="ensembl",
            observations=[
                Observation("same", [Measurement("a", MeasurementType.P_VALUE)]),
                Observation("same", [Measurement("b", MeasurementType.P_VALUE)]),
            ],
        )


def test_column_claimed_twice_rejected(frame):
    mapping = ColumnMapping(
        gene_id_column="Gene IDs",
        gene_id_type="ensembl",
        observations=[
            Observation("a", [Measurement("p treated", MeasurementType.P_VALUE)]),
            Observation("b", [Measurement("p treated", MeasurementType.P_VALUE)]),
        ],
    )
    with pytest.raises(MappingError, match="claimed more than once"):
        mapping.validate(frame)


def test_out_of_range_values_are_caught():
    bad = pd.DataFrame({"id": ["g1"], "p": [7.5]})
    mapping = ColumnMapping(
        gene_id_column="id",
        gene_id_type="ensembl",
        observations=[Observation("a", [Measurement("p", MeasurementType.P_VALUE)])],
    )
    with pytest.raises(MappingError, match="out-of-range"):
        mapping.validate(bad)
    mapping.validate(bad, check_ranges=False)  # opt out works


def test_fold_change_range_is_discontinuous():
    fc = MeasurementType.FOLD_CHANGE
    assert fc.is_plausible(1.0) and fc.is_plausible(-4.2)
    assert not fc.is_plausible(0.5)


def test_nan_is_always_allowed():
    assert MeasurementType.P_VALUE.is_plausible(float("nan"))


def test_from_blocks_matches_demo_layout():
    mapping = ColumnMapping.from_blocks(
        columns=["id", "fc1", "p1", "fc2", "p2"],
        gene_id_type="ensembl",
        observation_names=["obs A", "obs B"],
        measurement_types=["foldchange", "pvalue"],
        cutoffs=[1.5, None],
    )
    assert mapping.value_columns == ["fc1", "p1", "fc2", "p2"]
    assert mapping.cutoffs == [1.5, None]


def test_from_blocks_rejects_wrong_cutoff_count():
    with pytest.raises(MappingError, match="cutoff"):
        ColumnMapping.from_blocks(
            columns=["id", "fc1", "p1"],
            gene_id_type="ensembl",
            observation_names=["a"],
            measurement_types=["foldchange", "pvalue"],
            cutoffs=[1.5],
        )


def test_empty_dataset_rejected(two_obs_mapping, frame):
    with pytest.raises(MappingError, match="no rows"):
        Dataset.from_frame(frame.iloc[0:0], two_obs_mapping)


def test_fold_change_rejection_suggests_logratio_for_log_scale_data():
    """A column named "Fold_change" may hold log2 values; the data says which."""
    import numpy as np

    rng = np.random.default_rng(0)
    log2 = np.log2(rng.lognormal(0, 1, 300))     # centred on zero
    frame = pd.DataFrame({"id": [f"g{i}" for i in range(300)], "fc": log2})
    mapping = ColumnMapping(
        "id", "ensembl",
        [Observation("o", [Measurement("fc", MeasurementType.FOLD_CHANGE)])],
    )
    with pytest.raises(MappingError, match="declare it 'logratio'"):
        mapping.validate(frame)


def test_the_hint_mentions_both_signs_when_present():
    import numpy as np

    rng = np.random.default_rng(1)
    log2 = np.log2(rng.lognormal(0, 1, 300))
    frame = pd.DataFrame({"id": [f"g{i}" for i in range(300)], "fc": log2})
    mapping = ColumnMapping(
        "id", "ensembl",
        [Observation("o", [Measurement("fc", MeasurementType.FOLD_CHANGE)])],
    )
    try:
        mapping.validate(frame)
    except MappingError as exc:
        assert "with both signs" in str(exc)
        assert "0.72-fold" in str(exc)


def test_no_hint_when_the_data_is_simply_out_of_range():
    """Values outside (-1,1) that still fail are a different problem."""
    frame = pd.DataFrame({"id": ["g1", "g2"], "p": [7.5, 9.0]})
    mapping = ColumnMapping(
        "id", "ensembl",
        [Observation("o", [Measurement("p", MeasurementType.P_VALUE)])],
    )
    try:
        mapping.validate(frame)
    except MappingError as exc:
        assert "logratio" not in str(exc)
