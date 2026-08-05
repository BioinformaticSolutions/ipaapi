"""CLI argument parsing, 0-based column resolution, and identifier fallback."""

import argparse

import pandas as pd
import pytest

from ipaapi import ColumnMapping, Dataset, Measurement, MeasurementType, Observation
from ipaapi._payload import build_submission_pairs
from ipaapi.cli import build_mapping, build_parser, main, parse_fc_spec, parse_id_spec
from ipaapi.errors import IPAError, MappingError

COLUMNS = ["Gene IDs", "FoldChange", "PValue", "Symbol"]


# -- spec parsing ----------------------------------------------------------


def test_id_spec_parses_column_and_type():
    assert parse_id_spec("0:ensembl") == (0, "ensembl")
    assert parse_id_spec(" 3 : genesymbol ") == (3, "genesymbol")


def test_id_spec_requires_a_type():
    with pytest.raises(argparse.ArgumentTypeError, match="COLUMN:TYPE"):
        parse_id_spec("0")
    with pytest.raises(argparse.ArgumentTypeError, match="missing the identifier type"):
        parse_id_spec("0:")


def test_id_spec_rejects_non_integer_and_negative_columns():
    with pytest.raises(argparse.ArgumentTypeError, match="0-based integer"):
        parse_id_spec("first:ensembl")
    with pytest.raises(argparse.ArgumentTypeError, match="0 or greater"):
        parse_id_spec("-1:ensembl")


def test_fc_spec_with_and_without_cutoff():
    assert parse_fc_spec("1:foldchange") == (1, MeasurementType.FOLD_CHANGE, None)
    assert parse_fc_spec("1:foldchange:1.5") == (1, MeasurementType.FOLD_CHANGE, 1.5)
    assert parse_fc_spec("2:logratio") == (2, MeasurementType.LOG_RATIO, None)


def test_fc_spec_rejects_unknown_type_and_bad_cutoff():
    with pytest.raises(argparse.ArgumentTypeError, match="unknown measurement type"):
        parse_fc_spec("1:notatype")
    with pytest.raises(argparse.ArgumentTypeError, match="not a number"):
        parse_fc_spec("1:foldchange:high")


# -- column resolution -----------------------------------------------------


def test_columns_are_zero_based():
    mapping = build_mapping(COLUMNS, [(0, "ensembl")], (1, MeasurementType.FOLD_CHANGE, 1.5))
    assert mapping.gene_id_column == "Gene IDs"     # position 0
    assert mapping.observations[0].measurements[0].column == "FoldChange"  # position 1


def test_out_of_range_column_is_reported_with_the_available_positions():
    with pytest.raises(IPAError, match="only 4 column"):
        build_mapping(COLUMNS, [(9, "ensembl")], (1, MeasurementType.FOLD_CHANGE, None))


def test_observation_name_defaults_to_the_fc_column():
    mapping = build_mapping(COLUMNS, [(0, "ensembl")], (1, MeasurementType.FOLD_CHANGE, None))
    assert mapping.observations[0].name == "FoldChange"
    named = build_mapping(
        COLUMNS, [(0, "ensembl")], (1, MeasurementType.FOLD_CHANGE, None), "drug vs ctrl"
    )
    assert named.observations[0].name == "drug vs ctrl"


def test_fc_column_may_not_double_as_an_identifier():
    with pytest.raises(IPAError, match="already used as an identifier"):
        build_mapping(COLUMNS, [(0, "ensembl")], (0, MeasurementType.FOLD_CHANGE, None))


# -- two identifiers -------------------------------------------------------


def test_second_id_becomes_the_fallback():
    mapping = build_mapping(
        COLUMNS,
        [(0, "ensembl"), (3, "genesymbol")],
        (1, MeasurementType.FOLD_CHANGE, None),
    )
    assert mapping.gene_id_column == "Gene IDs"
    assert mapping.gene_id_type == "ensembl"
    assert mapping.gene_id_fallback_column == "Symbol"
    assert mapping.gene_id_fallback_type == "genesymbol"


def test_at_most_two_ids():
    with pytest.raises(IPAError, match="at most twice"):
        build_mapping(
            COLUMNS,
            [(0, "ensembl"), (3, "genesymbol"), (2, "refseq")],
            (1, MeasurementType.FOLD_CHANGE, None),
        )


def test_both_ids_may_not_be_the_same_column():
    with pytest.raises(IPAError, match="different column"):
        build_mapping(
            COLUMNS, [(0, "ensembl"), (0, "genesymbol")], (1, MeasurementType.FOLD_CHANGE, None)
        )


def test_fallback_only_fills_blank_primaries():
    frame = pd.DataFrame(
        {
            "Gene IDs": ["ENSG1", None, "", "ENSG4", "NA"],
            "FoldChange": [2.0, 2.0, 2.0, 2.0, 2.0],
            "PValue": [0.1] * 5,
            "Symbol": ["SYM1", "SYM2", "SYM3", "SYM4", None],
        }
    )
    mapping = build_mapping(
        list(frame.columns),
        [(0, "ensembl"), (3, "genesymbol")],
        (1, MeasurementType.FOLD_CHANGE, None),
    )
    resolved, filled = mapping.resolve_gene_ids(frame)
    # Rows 1 and 2 take the symbol. Row 4's primary is blank too, but its
    # fallback is also blank, so the primary's value is left untouched.
    assert list(resolved) == ["ENSG1", "SYM2", "SYM3", "ENSG4", "NA"]
    assert filled == 2


def test_fallback_counts_and_warnings_surface_on_the_dataset():
    frame = pd.DataFrame(
        {
            "id": ["ENSG1", None, None],
            "fc": [2.0, 2.0, 2.0],
            "sym": ["A", "B", None],
        }
    )
    mapping = ColumnMapping(
        gene_id_column="id",
        gene_id_type="ensembl",
        gene_id_fallback_column="sym",
        gene_id_fallback_type="genesymbol",
        observations=[Observation("o", [Measurement("fc", MeasurementType.FOLD_CHANGE)])],
    )
    dataset = Dataset.from_frame(frame, mapping)
    assert dataset.gene_ids_filled == 1
    assert dataset.gene_ids_missing == 1
    warnings = " ".join(dataset.id_warnings)
    assert "fallback" in warnings and "may not map" in warnings
    assert "no usable identifier" in warnings


def test_resolved_ids_are_what_actually_get_uploaded():
    frame = pd.DataFrame({"id": ["ENSG1", None], "fc": [2.0, 3.0], "sym": ["A", "B"]})
    mapping = ColumnMapping(
        gene_id_column="id",
        gene_id_type="ensembl",
        gene_id_fallback_column="sym",
        gene_id_fallback_type="genesymbol",
        observations=[Observation("o", [Measurement("fc", MeasurementType.FOLD_CHANGE)])],
    )
    pairs = build_submission_pairs(
        frame=frame,
        mapping=mapping,
        application_name="PythonAPI",
        project_name="P",
        dataset_name="D",
    )
    assert [v for k, v in pairs if k == "geneid"] == ["ENSG1", "B"]
    # IPA is still told exactly one gene ID type.
    assert [v for k, v in pairs if k == "geneidtype"] == ["ensembl"]


def test_fallback_requires_a_type():
    with pytest.raises(MappingError, match="gene_id_fallback_type must be set"):
        ColumnMapping(
            gene_id_column="id",
            gene_id_type="ensembl",
            gene_id_fallback_column="sym",
            observations=[Observation("o", [Measurement("fc", MeasurementType.FOLD_CHANGE)])],
        )


def test_all_identifiers_missing_is_an_error():
    frame = pd.DataFrame({"id": [None, None], "fc": [2.0, 3.0]})
    mapping = ColumnMapping(
        gene_id_column="id",
        gene_id_type="ensembl",
        observations=[Observation("o", [Measurement("fc", MeasurementType.FOLD_CHANGE)])],
    )
    with pytest.raises(MappingError, match="Every row is missing an identifier"):
        Dataset.from_frame(frame, mapping)


# -- parser wiring ---------------------------------------------------------


def test_id_and_fc_are_required():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["submit", "f.txt", "--project", "P"])
    with pytest.raises(SystemExit):
        parser.parse_args(["submit", "f.txt", "--ID", "0:ensembl", "--project", "P"])


def test_repeated_id_flags_accumulate():
    args = build_parser().parse_args(
        ["validate", "f.txt", "--ID", "0:ensembl", "--ID", "3:genesymbol", "--FC", "1:foldchange"]
    )
    assert args.ID == [(0, "ensembl"), (3, "genesymbol")]
    assert args.FC == (1, MeasurementType.FOLD_CHANGE, None)


def test_bare_invocation_exits_nonzero():
    assert main([]) == 2


# -- waiting behaviour -----------------------------------------------------


def test_submit_does_not_wait_by_default():
    args = build_parser().parse_args(
        ["submit", "f.txt", "--ID", "0:ensembl", "--FC", "1:foldchange", "--project", "P"]
    )
    assert args.wait is False


def test_wait_is_opt_in():
    args = build_parser().parse_args(
        [
            "submit", "f.txt", "--ID", "0:ensembl", "--FC", "1:foldchange",
            "--project", "P", "--wait",
        ]
    )
    assert args.wait is True


def test_old_no_wait_flag_still_parses():
    """--no-wait became the default; accepting it keeps existing commands working."""
    args = build_parser().parse_args(
        [
            "submit", "f.txt", "--ID", "0:ensembl", "--FC", "1:foldchange",
            "--project", "P", "--no-wait",
        ]
    )
    assert args.wait is False


# -- versioning ------------------------------------------------------------


def test_version_is_single_sourced_from_the_package():
    """pyproject reads __version__ from __init__.py; nothing declares it twice."""
    import pathlib
    import re

    import ipaapi

    root = pathlib.Path(ipaapi.__file__).resolve().parent.parent.parent
    pyproject = (root / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert "[tool.hatch.version]" in pyproject
    assert not re.search(r"^version = ", pyproject, re.MULTILINE)

    # The pattern hatchling uses must actually find it.
    src = (root / "src" / "ipaapi" / "__init__.py").read_text()
    found = re.search(
        r"^__version__\s*(?::.*)?=\s*(['\"])(?P<version>.+?)\1", src, re.MULTILINE
    )
    assert found and found.group("version") == ipaapi.__version__


def test_version_banner_identifies_the_installation():
    from ipaapi.cli import version_banner

    banner = version_banner()
    import ipaapi

    assert ipaapi.__version__ in banner
    assert "installed at" in banner
    assert "python" in banner


def test_changelog_documents_the_current_version():
    import pathlib

    import ipaapi

    root = pathlib.Path(ipaapi.__file__).resolve().parent.parent.parent
    changelog = (root / "CHANGELOG.md").read_text()
    assert f"## {ipaapi.__version__}" in changelog
