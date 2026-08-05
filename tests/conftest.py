import pandas as pd
import pytest

from ipaapi import ColumnMapping, Measurement, MeasurementType, Observation


@pytest.fixture
def frame():
    """A deliberately awkward table: interleaved columns, spaces, junk column."""
    return pd.DataFrame(
        {
            "Gene IDs": ["ENSG1", "ENSG2", "ENSG3"],
            "notes": ["ignore", "me", "entirely"],
            "FC treated": [2.0, -3.0, 1.5],
            "p treated": [0.01, 0.2, 0.5],
            "FC control": [1.2, -1.1, 4.0],
            "p control": [0.05, 0.6, 0.001],
        }
    )


@pytest.fixture
def two_obs_mapping():
    """Two observations whose columns are declared in different orders."""
    return ColumnMapping(
        gene_id_column="Gene IDs",
        gene_id_type="ensembl",
        observations=[
            Observation(
                "treated vs ctrl",
                [
                    Measurement("FC treated", MeasurementType.FOLD_CHANGE, cutoff=1.5),
                    Measurement("p treated", MeasurementType.P_VALUE),
                ],
            ),
            # Same types, declared in the opposite order on purpose.
            Observation(
                "control vs ctrl",
                [
                    Measurement("p control", MeasurementType.P_VALUE),
                    Measurement("FC control", MeasurementType.FOLD_CHANGE, cutoff=1.5),
                ],
            ),
        ],
    )


_ROWS = "id\tfc\nENSG1\t2.0\nENSG2\t-3.0\n"


@pytest.fixture
def tree():
    """A scratch directory of dataset files, some of which should be ignored."""
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "SampleA_DEG.txt").write_text(_ROWS)
    (tmp / "SampleB_DEG.txt").write_text(_ROWS)
    (tmp / "SampleA_raw.tsv").write_text(_ROWS)
    (tmp / "notes.md").write_text("not a table")
    (tmp / ".hidden.txt").write_text(_ROWS)
    nested = tmp / "groupB"
    nested.mkdir()
    (nested / "SampleC_DEG.txt").write_text(_ROWS)
    return tmp
