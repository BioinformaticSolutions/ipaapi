"""Observation names: keeping the meaning while getting under IPA's limit.

IPA rejects a long ``obs1name`` and reports it as "The page you are looking for
is currently unavailable" -- an outage page, for a parameter error. So names
have to be shortened. But an observation name is also the label IPA shows for a
column and puts side by side in a comparison analysis, so *what* is removed
matters as much as how much.

Two things in a pipeline filename carry meaning: the contrast
(``Estrus_vs_2dpp``) and what it was computed from
(``Immature_cortical_ovarian_stroma``). Everything the pipeline appends about
how it ran is disposable. These tests pin that priority.
"""

from ipaapi.cli import (
    MAX_OBSERVATION_NAME,
    MIN_STRIPPED_NAME,
    drop_disposable_suffix,
    observation_names,
    shared_affixes,
    strip_paralome_suffix,
    strip_shared_prefix,
    trim_name,
)

# Real Paralome output: two different contrasts, two different pipeline
# suffixes, several cell types.
REAL_BATCH = [
    "Estrus_vs_2dpp_Immature_cortical_ovarian_stroma_naive_cell_t_significant_p0.05_rna",
    "Estrus_vs_2dpp_Glandular_epithelium_pseudobulk_t_significant_p0.05_rna",
    "Estrus_vs_2dpp_Luminal_epithelium_secretory_naive_cell_t_significant_p0.05_rna",
    "Diestrus_vs_2dpp_Mature_cortical_ovarian_stroma_pseudobulk_t_significant_p0.05_rna",
]

EXPECTED = [
    "Estrus_vs_2dpp_Immature_cortical_ovarian_stroma",
    "Estrus_vs_2dpp_Glandular_epithelium",
    "Estrus_vs_2dpp_Luminal_epithelium_secretory",
    "Diestrus_vs_2dpp_Mature_cortical_ovarian_stroma",
]


def test_contrast_and_cell_type_both_survive():
    assert observation_names(REAL_BATCH) == EXPECTED


def test_a_single_file_gets_the_same_treatment():
    # Pipeline suffixes are recognised from the tokens themselves, so one file
    # does not need a batch to compare against.
    assert observation_names([REAL_BATCH[0]]) == [EXPECTED[0]]


def test_nothing_is_truncated_when_metadata_removal_is_enough():
    assert all(".." not in name for name in observation_names(REAL_BATCH))


def test_short_names_are_never_rewritten():
    names = ["SampleA_DEG", "SampleB_DEG"]
    assert observation_names(names) == names


def test_everything_ends_up_within_the_limit():
    assert all(len(n) <= MAX_OBSERVATION_NAME for n in observation_names(REAL_BATCH))


def test_names_stay_distinct():
    out = observation_names(REAL_BATCH)
    assert len(set(out)) == len(REAL_BATCH)


def test_disposable_suffix_stops_at_the_first_real_word():
    assert (
        drop_disposable_suffix(
            "Estrus_vs_2dpp_Luminal_epithelium_secretory_naive_cell_t_significant_p0.05_rna"
        )
        == "Estrus_vs_2dpp_Luminal_epithelium_secretory"
    )


def test_cutoff_tokens_are_recognised_in_several_shapes():
    for tail in ("_p0.05", "_fdr0.01", "_padj0.05", "_0.05"):
        assert drop_disposable_suffix("Contrast_Stroma" + tail) == "Contrast_Stroma"


def test_a_name_of_nothing_but_metadata_is_left_alone():
    # Better a useless name than an empty one.
    assert drop_disposable_suffix("significant_p0.05_rna") == "significant_p0.05_rna"


def test_the_shared_prefix_is_only_taken_as_a_last_resort():
    # The contrast lives at the front, so it survives whenever removing
    # pipeline metadata alone gets the names under the limit.
    assert all(n.count("_vs_") == 1 for n in observation_names(REAL_BATCH))


def test_the_shared_prefix_is_taken_when_there_is_no_other_choice():
    names = ["Estrus_vs_2dpp_" + "Very_" * 14 + "long_" + tissue for tissue in ("stroma", "epithelium")]
    out = observation_names(names)
    assert all(len(n) <= MAX_OBSERVATION_NAME for n in out)
    assert len(set(out)) == 2


def test_explicit_strip_runs_before_everything():
    names = ["Estrus_vs_2dpp_Stroma_customsuffix_" + "x" * 40,
             "Estrus_vs_2dpp_Gland_customsuffix_" + "x" * 40]
    out = observation_names(names, strip=["_customsuffix_" + "x" * 40])
    assert out == ["Estrus_vs_2dpp_Stroma", "Estrus_vs_2dpp_Gland"]


def test_strip_is_ignored_when_it_would_collide():
    names = ["Estrus_Stroma_A", "Estrus_Stroma_B"]
    assert observation_names(names, strip=["_A", "_B"]) == names


def test_shared_affixes_compares_whole_tokens():
    # 'Mature' must not be read as a prefix of 'Immature'.
    prefix, _ = shared_affixes(["Mature_stroma_x", "Immature_stroma_x"])
    assert prefix == []


def test_strip_shared_prefix_leaves_names_distinct():
    assert strip_shared_prefix(["a_x", "a_x"]) == ["a_x", "a_x"]


def test_trim_keeps_both_ends_and_marks_the_gap():
    out = trim_name("Contrast_" + "Middle_" * 20 + "Celltype")
    assert out.startswith("Contrast")
    assert out.endswith("Celltype")
    assert ".." in out
    assert len(out) <= MAX_OBSERVATION_NAME


def test_trim_does_not_cut_mid_word():
    # The first version of this turned 'cell_type' into 'cell_t'.
    name = "Estrus_vs_2dpp_" + "Immature_cortical_ovarian_" * 3 + "naive_cell_type"
    out = trim_name(name)
    for fragment in out.replace("..", "_").split("_"):
        assert not fragment or fragment in name.split("_")


def test_a_single_token_longer_than_the_limit_is_still_cut():
    assert len(trim_name("x" * 200)) <= MAX_OBSERVATION_NAME


def test_min_stripped_name_is_enforced():
    assert MIN_STRIPPED_NAME >= 3


def test_cap_sits_below_the_known_good_length():
    # 65 characters was accepted live and 82 rejected, so the cap has to sit
    # under the known-good value, not merely under the known-bad one.
    assert MAX_OBSERVATION_NAME < 65


def test_strip_does_not_short_circuit_metadata_removal():
    # --strip freeing just enough room must not stop the pipeline suffix from
    # being removed as well, which would make the flag actively harmful.
    with_flag = observation_names(REAL_BATCH, strip=["_significant_p0.05_rna"])
    assert with_flag == observation_names(REAL_BATCH) == EXPECTED


# -- Paralome's own naming convention --------------------------------------
#
# Paralome writes
# <contrast>_<celltype>_<method>_<test>_significant_<threshold>_<assay>.
# Anchoring on the 'significant' literal makes the cut exact, and means no list
# of statistical test names is needed -- the test is the token before it.


def test_paralome_cut_keeps_contrast_and_cell_type():
    assert strip_paralome_suffix(REAL_BATCH[0]) == EXPECTED[0]
    assert strip_paralome_suffix(REAL_BATCH[1]) == EXPECTED[1]


def test_the_test_name_needs_no_vocabulary():
    # 't' and 'wilcox' are both simply "the token before the anchor".
    assert (
        strip_paralome_suffix(
            "Estrus_vs_2dpp_Stroma_pseudobulk_wilcox_significant_fdr0.01_rna"
        )
        == "Estrus_vs_2dpp_Stroma"
    )


def test_a_cell_type_sharing_words_with_a_method_survives():
    # 'Naive_T_cell' contains both words of the 'naive_cell' method. Matching
    # loose tokens rather than whole phrases cut this to 'Estrus_vs_2dpp_Naive_T'.
    assert (
        strip_paralome_suffix("Estrus_vs_2dpp_Naive_T_cell_pseudobulk_t_significant_p0.05_rna")
        == "Estrus_vs_2dpp_Naive_T_cell"
    )


def test_a_cell_type_directly_abutting_its_own_method_name():
    assert (
        strip_paralome_suffix("Estrus_vs_2dpp_Naive_T_cell_naive_cell_t_significant_p0.05_rna")
        == "Estrus_vs_2dpp_Naive_T_cell"
    )


def test_only_one_method_phrase_is_taken():
    # Guards against walking backwards through the cell type one word at a time.
    name = "Estrus_vs_2dpp_bulk_bulk_pseudobulk_t_significant_p0.05_rna"
    assert strip_paralome_suffix(name) == "Estrus_vs_2dpp_bulk_bulk"


def test_a_non_paralome_name_is_left_to_the_generic_rules():
    assert strip_paralome_suffix("SampleA_DEG") == "SampleA_DEG"


def test_an_anchor_with_nothing_in_front_is_refused():
    assert strip_paralome_suffix("significant_p0.05_rna") == "significant_p0.05_rna"


def test_the_last_anchor_wins():
    # A cell type containing the word would otherwise cut far too early.
    name = "Estrus_vs_2dpp_significant_stroma_pseudobulk_t_significant_p0.05_rna"
    assert strip_paralome_suffix(name) == "Estrus_vs_2dpp_significant_stroma"
