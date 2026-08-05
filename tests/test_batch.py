"""Directory search, --pattern semantics, and batch validation."""

import pathlib

import pytest

from ipaapi.cli import (
    TABLE_PATTERNS,
    _load_datasets,
    build_parser,
    discover_files,
    expand_pattern,
)
from ipaapi.errors import IPAError
from ipaapi.models import MeasurementType

# -- pattern semantics -----------------------------------------------------


def test_plain_text_matches_as_a_substring():
    assert expand_pattern("SampleA") == ["*SampleA*"]


def test_glob_characters_are_used_verbatim():
    assert expand_pattern("*_DEG.txt") == ["*_DEG.txt"]
    assert expand_pattern("Sample?.txt") == ["Sample?.txt"]


def test_no_pattern_searches_table_extensions():
    assert expand_pattern(None) == list(TABLE_PATTERNS)
    assert expand_pattern("  ") == list(TABLE_PATTERNS)


# -- discovery -------------------------------------------------------------


def test_single_file_is_returned_directly(tree):
    target = tree / "SampleA_DEG.txt"
    assert discover_files(str(target)) == [target]


def test_directory_defaults_to_table_extensions(tree):
    names = [p.name for p in discover_files(str(tree))]
    assert names == ["SampleA_DEG.txt", "SampleA_raw.tsv", "SampleB_DEG.txt"]
    assert "notes.md" not in names       # wrong extension
    assert ".hidden.txt" not in names    # hidden files skipped


def test_substring_pattern_narrows_the_set(tree):
    names = [p.name for p in discover_files(str(tree), pattern="SampleA")]
    assert names == ["SampleA_DEG.txt", "SampleA_raw.tsv"]


def test_glob_pattern_narrows_the_set(tree):
    names = [p.name for p in discover_files(str(tree), pattern="*_DEG.txt")]
    assert names == ["SampleA_DEG.txt", "SampleB_DEG.txt"]


def test_recursive_reaches_subdirectories(tree):
    flat = [p.name for p in discover_files(str(tree), pattern="*_DEG.txt")]
    deep = [p.name for p in discover_files(str(tree), pattern="*_DEG.txt", recursive=True)]
    assert "SampleC_DEG.txt" not in flat
    assert "SampleC_DEG.txt" in deep


def test_results_are_sorted(tree):
    found = discover_files(str(tree), pattern="*_DEG.txt", recursive=True)
    assert found == sorted(found)


def test_no_match_lists_what_is_there(tree):
    with pytest.raises(IPAError, match="Files present"):
        discover_files(str(tree), pattern="Nothing")


def test_no_match_suggests_recursive_when_not_already_recursive(tree):
    with pytest.raises(IPAError, match="--recursive"):
        discover_files(str(tree), pattern="Nothing")


def test_missing_path_is_reported(tree):
    with pytest.raises(IPAError, match="No such file or directory"):
        discover_files(str(tree / "nope"))


# -- batch loading ---------------------------------------------------------


def parse(tree, *extra):
    return build_parser().parse_args(
        ["validate", str(tree), "--ID", "0:ensembl", "--FC", "1:foldchange", *extra]
    )


def test_each_file_becomes_its_own_dataset_named_after_the_file(tree):
    datasets = _load_datasets(parse(tree, "--pattern", "*_DEG.txt"))
    assert [d.name for d in datasets] == ["SampleA_DEG", "SampleB_DEG"]
    # Observation name comes from the filename, not the FC column header.
    assert datasets[0].mapping.observations[0].name == "SampleA_DEG"


def test_single_file_still_works(tree):
    datasets = _load_datasets(parse(tree / "SampleA_DEG.txt"))
    assert len(datasets) == 1
    assert datasets[0].name == "SampleA_DEG"


def test_observation_override_rejected_for_multiple_files(tree):
    with pytest.raises(IPAError, match="--observation applies to a single file"):
        _load_datasets(parse(tree, "--pattern", "*_DEG.txt", "--observation", "one name"))


def test_one_bad_file_aborts_the_whole_batch(tree):
    (tree / "SampleZ_DEG.txt").write_text("id\nENSG1\n")  # no fold-change column
    with pytest.raises(IPAError, match="nothing was uploaded"):
        _load_datasets(parse(tree, "--pattern", "*_DEG.txt"))


def test_the_offending_file_is_named(tree):
    (tree / "SampleZ_DEG.txt").write_text("id\nENSG1\n")
    with pytest.raises(IPAError, match="SampleZ_DEG.txt"):
        _load_datasets(parse(tree, "--pattern", "*_DEG.txt"))


def test_pattern_and_recursive_reach_the_parser(tree):
    args = parse(tree, "--pattern", "SampleA", "--recursive")
    assert args.pattern == "SampleA"
    assert args.recursive is True
    assert args.FC == (1, MeasurementType.FOLD_CHANGE, None)
