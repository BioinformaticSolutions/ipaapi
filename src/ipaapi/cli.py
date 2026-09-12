"""Command-line interface for :mod:`ipaapi`.

Installed as the ``ipaapi`` console script::

    ipaapi --help
    ipaapi submit data.txt --ID 0:ensembl --FC 1:foldchange --project MyProject

Column positions are **0-based**: ``--ID 0`` is the first column in the file.
"""

from __future__ import annotations

import argparse
import fnmatch
import pathlib
import re
import sys
from typing import List, Optional, Sequence, Tuple

from . import __version__, history
from .dataset import Dataset, load_table
from .errors import (
    AnalysisRefusedError,
    IPAError,
    MalformedRequestError,
    QuotaExceededError,
    ServiceUnavailableError,
)
from .mapping import ColumnMapping, Measurement, Observation
from .models import GENE_ID_TYPES, MeasurementType, ReferenceSet
from .triage import TRIAGE_DIRNAMES, Triage

__all__ = ["main"]

#: Extensions searched in a directory when no --pattern is given.
TABLE_PATTERNS = ("*.txt", "*.tsv", "*.csv")

_GLOB_CHARS = set("*?[")

# The accepted gene ID vocabulary is documented (IPA Integration Module, April
# 2026, section 3.1) and lives in models.GENE_ID_TYPES. Nothing is guessed here
# any more: an earlier hand-made list of "common" types walked users straight
# into failed submissions, because plausible names like 'genesymbol' and 'hgnc'
# are not accepted while 'hugo' is.

#: Longest observation name observed to be accepted by IPA.
#:
#: A long ``obs1name`` is rejected, and reported as "The page you are looking
#: for is currently unavailable" -- wording that reads as an outage and cost a
#: full working day to attribute correctly. Established by A/B: with the same
#: file, project and data, a 25-character observation name was accepted while
#: an 82-character one was rejected; a long *dataset* name in the same request
#: was fine, so the limit is specific to the observation.
#:
#: 65 characters is known good and 82 known bad, so the true limit lies between.
#: The cap is set below the known-good value to leave room, since IPA gives no
#: usable diagnostic when it is exceeded.
MAX_OBSERVATION_NAME = 60

#: A short list for help text; the full mapping is in GENE_ID_TYPES.
COMMON_ID_TYPES = ("ensembl", "hugo", "entrezgene", "refseq", "swissprot")

_EPILOG = f"""\
column positions are 0-based: --ID 0 is the first column in the file

--ID may be given up to twice. The first is the primary identifier and is what
IPA is told the gene ID type is. The second, if present, is used only for rows
where the primary is blank. Because IPA accepts one gene ID type per submission,
rows filled from a second identifier of a different type are uploaded under the
primary's type and may not map; the fill count is always reported.

common gene ID types: {', '.join(COMMON_ID_TYPES)}
Species is carried by the ID type, not a separate parameter: hugo is human,
mousesymeg mouse, ratsymeg rat. Note 'genesymbol' and 'hgnc' are NOT accepted
for gene symbols -- the value is 'hugo'. Run with --list-id-types for all of
them.
measurement types for --FC: ratio, foldchange, logratio

PATH may be a single file or a directory. Given a directory, --pattern selects
which files to use and each matched file is submitted as its own dataset and
analysis, named after the file. Every file must fit the same --ID/--FC layout;
all of them are validated before any is uploaded.

examples:
  ipaapi validate rnaseq.txt --ID 0:ensembl --FC 1:foldchange
  ipaapi submit rnaseq.txt --ID 0:ensembl --FC 1:foldchange:1.5 --project Study1
  ipaapi submit rnaseq.txt --ID 0:ensembl --ID 4:hugo \\
      --FC 1:foldchange --project Study1

  # every .txt/.tsv/.csv in a folder, one analysis each
  ipaapi submit ~/data --ID 0:ensembl --FC 1:foldchange --project Study1

  # only files whose name contains SampleA
  ipaapi validate ~/data --pattern SampleA --ID 0:ensembl --FC 1:foldchange

  # glob, searching subfolders too
  ipaapi submit ~/data --pattern "*_DEG.tsv" --recursive \\
      --ID 0:ensembl --FC 1:foldchange --project GroupB

  # submit returns as soon as the analyses are queued; --wait blocks instead
  ipaapi submit rnaseq.txt --ID 0:ensembl --FC 1:foldchange --project Study1 --wait

  ipaapi status abc-123 abc-124
  ipaapi report abc-123 --open

  # every analysis you have submitted through this tool, with current status
  ipaapi history --status
"""


class _Formatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter
):
    """Show defaults, but leave the epilog's line breaks alone."""


def version_banner() -> str:
    """Version plus enough context to identify *which* install is running.

    With several machines and hand-built wheels in play, the version number
    alone does not answer "am I running the build I think I am". The install
    path and interpreter do.
    """
    import sys as _sys

    location = pathlib.Path(__file__).resolve().parent
    lines = [
        f"ipaapi {__version__}",
        f"installed at {location}",
        f"python {_sys.version.split()[0]} ({_sys.executable})",
    ]

    # An editable install runs straight from a checkout, where the working tree
    # may be ahead of the version number. Say so rather than let it mislead.
    if (location.parent.parent / ".git").exists():
        lines.append("running from a source checkout (editable install)")
    return "\n".join(lines)


# -- argument specs --------------------------------------------------------


def parse_id_spec(text: str) -> Tuple[int, str]:
    """Parse ``COLUMN:TYPE`` for ``--ID``, e.g. ``0:ensembl``."""
    parts = text.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"--ID expects COLUMN:TYPE (for example 0:ensembl), got {text!r}."
        )
    column, id_type = parts[0].strip(), parts[1].strip()
    if not id_type:
        raise argparse.ArgumentTypeError(
            f"--ID {text!r} is missing the identifier type, e.g. "
            f"{text.rstrip(':')}:{COMMON_ID_TYPES[0]}."
        )
    if id_type.lower() not in GENE_ID_TYPES:
        close = [t for t in GENE_ID_TYPES if id_type.lower() in t or t in id_type.lower()]
        hint = f" Did you mean: {', '.join(sorted(close))}?" if close else ""
        print(
            f"Warning: {id_type!r} is not in IPA's documented gene ID type list."
            + hint
            + " Sending it anyway; run 'ipaapi submit --list-id-types' for the"
            " full list.",
            file=sys.stderr,
        )
    return _parse_index(column, "--ID"), id_type


def parse_fc_spec(text: str) -> Tuple[int, MeasurementType, Optional[float]]:
    """Parse ``COLUMN:TYPE[:CUTOFF]`` for ``--FC``, e.g. ``1:foldchange:1.5``."""
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(
            f"--FC expects COLUMN:TYPE[:CUTOFF] (for example 1:foldchange:1.5), "
            f"got {text!r}."
        )
    index = _parse_index(parts[0].strip(), "--FC")

    raw_type = parts[1].strip().lower()
    try:
        measurement = MeasurementType(raw_type)
    except ValueError:
        allowed = ", ".join(m.value for m in MeasurementType)
        raise argparse.ArgumentTypeError(
            f"--FC has unknown measurement type {raw_type!r}. Allowed: {allowed}."
        ) from None

    cutoff = None
    if len(parts) == 3 and parts[2].strip():
        try:
            cutoff = float(parts[2])
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--FC cutoff {parts[2]!r} is not a number."
            ) from None
    return index, measurement, cutoff


def parse_stat_spec(text: str, flag: str) -> Tuple[int, Optional[float]]:
    """Parse ``COLUMN[:CUTOFF]`` for --pvalue and --fdr.

    No type component, unlike --FC: the flag name fixes the measurement type,
    which is the point of having separate flags rather than making people spell
    out ``--FC 5:pvalue``.
    """
    parts = text.split(":")
    if len(parts) not in (1, 2):
        raise argparse.ArgumentTypeError(
            f"{flag} expects COLUMN[:CUTOFF] (for example 5:0.05), got {text!r}."
        )
    index = _parse_index(parts[0].strip(), flag)
    cutoff = None
    if len(parts) == 2 and parts[1].strip():
        try:
            cutoff = float(parts[1])
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{flag} cutoff {parts[1]!r} is not a number."
            ) from None
    return index, cutoff


def parse_pvalue_spec(text: str) -> Tuple[int, Optional[float]]:
    return parse_stat_spec(text, "--pvalue")


def parse_fdr_spec(text: str) -> Tuple[int, Optional[float]]:
    return parse_stat_spec(text, "--fdr")


def _parse_index(text: str, flag: str) -> int:
    try:
        index = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{flag} column must be a 0-based integer position, got {text!r}."
        ) from None
    if index < 0:
        raise argparse.ArgumentTypeError(
            f"{flag} column must be 0 or greater (positions are 0-based), got {index}."
        )
    return index


def _column_at(columns: Sequence[str], index: int, flag: str) -> str:
    """Resolve a 0-based position to a column name, with a legible error."""
    if index >= len(columns):
        listing = ", ".join(f"{i}={c!r}" for i, c in enumerate(columns[:12]))
        raise IPAError(
            f"{flag} refers to column {index}, but the file has only {len(columns)} "
            f"column(s) (positions 0-{len(columns) - 1}). Columns: {listing}"
            + (" ..." if len(columns) > 12 else "")
        )
    return columns[index]


# -- mapping construction --------------------------------------------------


def build_mapping(
    columns: Sequence[str],
    id_specs: List[Tuple[int, str]],
    fc_spec: Tuple[int, MeasurementType, Optional[float]],
    observation_name: Optional[str] = None,
    pvalue_spec: Optional[Tuple[int, Optional[float]]] = None,
    fdr_spec: Optional[Tuple[int, Optional[float]]] = None,
) -> ColumnMapping:
    """Turn parsed CLI specs into a :class:`ColumnMapping`.

    Measurement order is fixed -- fold change, then p-value, then FDR -- because
    the wire format declares the measurement slots once for the whole
    submission and then fills them positionally. A stable order keeps the
    encoding reproducible.
    """
    if not id_specs:
        raise IPAError("--ID is required.")
    if len(id_specs) > 2:
        raise IPAError(
            f"--ID may be given at most twice (primary and fallback); got "
            f"{len(id_specs)}."
        )

    primary_index, primary_type = id_specs[0]
    primary_column = _column_at(columns, primary_index, "--ID")

    fallback_column = fallback_type = None
    if len(id_specs) == 2:
        fallback_index, fallback_type = id_specs[1]
        fallback_column = _column_at(columns, fallback_index, "--ID")
        if fallback_index == primary_index:
            raise IPAError(
                f"Both --ID flags refer to column {primary_index} "
                f"({primary_column!r}); the fallback must be a different column."
            )

    fc_index, fc_type, fc_cutoff = fc_spec
    fc_column = _column_at(columns, fc_index, "--FC")
    if fc_column in (primary_column, fallback_column):
        raise IPAError(
            f"--FC refers to column {fc_index} ({fc_column!r}), which is already "
            "used as an identifier column."
        )

    measurements = [Measurement(fc_column, fc_type, cutoff=fc_cutoff)]
    taken = {primary_column, fallback_column, fc_column}

    for spec, kind, flag in (
        (pvalue_spec, MeasurementType.P_VALUE, "--pvalue"),
        (fdr_spec, MeasurementType.FALSE_DISCOVERY, "--fdr"),
    ):
        if spec is None:
            continue
        index, cutoff = spec
        column = _column_at(columns, index, flag)
        if column in taken:
            raise IPAError(
                f"{flag} refers to column {index} ({column!r}), which is already "
                "used by another flag. Each column may be claimed once."
            )
        taken.add(column)
        measurements.append(Measurement(column, kind, cutoff=cutoff))

    return ColumnMapping(
        gene_id_column=primary_column,
        gene_id_type=primary_type,
        gene_id_fallback_column=fallback_column,
        gene_id_fallback_type=fallback_type,
        observations=[
            Observation(
                name=observation_name or fc_column,
                measurements=measurements,
            )
        ],
    )


# -- file discovery --------------------------------------------------------


def expand_pattern(pattern: Optional[str]) -> List[str]:
    """Turn a ``--pattern`` value into fnmatch patterns.

    Plain search text with no glob characters is treated as a substring, so
    ``--pattern SampleA`` matches ``SampleA_DEG.txt``. Anything containing
    ``*``, ``?`` or ``[`` is used verbatim. With no pattern at all, the common
    delimited-text extensions are searched.
    """
    if pattern is None:
        return list(TABLE_PATTERNS)
    pattern = pattern.strip()
    if not pattern:
        return list(TABLE_PATTERNS)
    if any(char in pattern for char in _GLOB_CHARS):
        return [pattern]
    return [f"*{pattern}*"]


def discover_files(
    path: str, pattern: Optional[str] = None, recursive: bool = False
) -> List[pathlib.Path]:
    """Return the files to submit, sorted for a predictable run order.

    *path* may be a single file, in which case it is returned as-is, or a
    directory to search.
    """
    root = pathlib.Path(path).expanduser()
    if root.is_file():
        return [root]
    if not root.exists():
        raise IPAError(f"No such file or directory: {str(root)!r}")
    if not root.is_dir():
        raise IPAError(f"Not a readable file or directory: {str(root)!r}")

    patterns = expand_pattern(pattern)
    candidates = root.rglob("*") if recursive else root.glob("*")
    matched = sorted(
        candidate
        for candidate in candidates
        if candidate.is_file()
        and not candidate.name.startswith(".")
        # Files already filed into submitted/ or failed/ are not input, or a
        # second run would resubmit work that succeeded the first time.
        and not (TRIAGE_DIRNAMES & set(candidate.relative_to(root).parts[:-1]))
        and any(fnmatch.fnmatch(candidate.name, pat) for pat in patterns)
    )

    if not matched:
        present = sorted(
            child.name for child in root.iterdir() if child.is_file()
        )[:10]
        raise IPAError(
            f"No files in {str(root)!r} matched {', '.join(repr(p) for p in patterns)}"
            + (" (searched recursively)" if recursive else "")
            + (
                "\nFiles present: " + ", ".join(repr(n) for n in present)
                if present
                else "\nThe directory contains no files."
            )
            + ("\nUse --recursive to search subdirectories." if not recursive else "")
        )
    return matched


# -- dataset loading -------------------------------------------------------

_SINGLE_FILE_FLAGS = ("observation", "analysis_name", "dataset_name")


def _already_submitted(project: str, dataset_name: str, log_file) -> Optional[dict]:
    """Return the earlier submission of this dataset to this project, if any.

    IPA refuses a dataset whose name already exists in a project, and reports it
    as "The page you are looking for is currently unavailable" -- wording that
    reads as an outage and sends people looking in entirely the wrong place.
    Since every submission is logged locally, the collision can be predicted
    rather than walked into.
    """
    for row in history.read(log_file):
        if row.get("project") == project and row.get("dataset_name") == dataset_name:
            return row
    return None


#: Characters treated as word boundaries when trimming a name, so a cut never
#: lands mid-token and turns "cell_type" into "cell_t".
#:
#: A period is deliberately not one of them: it appears *inside* tokens like
#: "p0.05", and splitting there leaves "p0" and "05", neither recognisable as
#: the cutoff it came from.
_SPLIT_SEPARATORS = "_- "

#: Characters to clean off the ends of a name once something has been removed.
#: A period belongs here even though it is not a split point, since a name
#: should not be left starting or ending with one.
_NAME_SEPARATORS = "_-. "


def _split_tokens(name: str) -> List[str]:
    """Split on separators, keeping them, so a name can be rebuilt exactly."""
    tokens: List[str] = []
    current = ""
    for char in name:
        if char in _SPLIT_SEPARATORS:
            if current:
                tokens.append(current)
                current = ""
            tokens.append(char)
        else:
            current += char
    if current:
        tokens.append(current)
    return tokens


def shared_affixes(names: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Return the leading and trailing tokens common to *every* name.

    Boilerplate is what the batch has in common: a pipeline that writes
    ``Estrus_vs_2dpp_<tissue>_significant_p0.05_rna.csv`` puts the same words at
    both ends of every file and the distinguishing content in the middle. Those
    ends are exactly what can be dropped without losing the ability to tell two
    observations apart.

    Comparison is token-wise, so a partial word is never treated as shared.
    """
    if len(names) < 2:
        return [], []

    token_lists = [_split_tokens(name) for name in names]
    shortest = min(len(t) for t in token_lists)

    prefix: List[str] = []
    for index in range(shortest):
        token = token_lists[0][index]
        if all(t[index] == token for t in token_lists):
            prefix.append(token)
        else:
            break

    suffix: List[str] = []
    for index in range(1, shortest - len(prefix) + 1):
        token = token_lists[0][-index]
        if all(t[-index] == token for t in token_lists):
            suffix.append(token)
        else:
            break
    suffix.reverse()
    return prefix, suffix


#: A stripped name shorter than this is treated as having lost its meaning --
#: reducing files to "1" and "2" keeps them distinct but makes the analysis
#: unreadable, which defeats the purpose.
MIN_STRIPPED_NAME = 4

#: Trailing tokens that describe how a file was *processed* rather than what it
#: contains, and so can be dropped when a name has to be shortened.
#:
#: Analysis pipelines append their settings to the filename -- Paralome writes
#: ``_naive_cell_t_significant_p0.05_rna`` and ``_pseudobulk_t_significant_p0.05_rna``
#: -- while the front of the name carries the experimental design: the contrast
#: and the cell type it was computed from. Those two are what a reader needs and
#: what must survive.
#:
#: Only consulted when a name is over the limit, and everything removed is
#: printed. Use --strip for a pipeline whose suffixes are not covered here.
DISPOSABLE_TOKENS = frozenset(
    {
        "significant", "sig", "signif",
        "pseudobulk", "bulk", "sc", "singlecell",
        "naive", "cell", "cells", "celltype",
        "t", "wilcox", "wilcoxon", "deseq", "deseq2", "edger", "limma",
        "rna", "atac", "dge", "deg", "degs",
        "filtered", "filter", "results", "result", "output", "out",
        "padj", "pval", "pvalue", "fdr", "qval", "adj",
        "up", "down", "all",
    }
)

#: A trailing cutoff such as ``p0.05``, ``fdr0.01`` or ``0.05``.
_CUTOFF_TOKEN = re.compile(r"^(p|q|padj|fdr|adj|log2fc|fc|lfc)?[0-9]*\.?[0-9]+$", re.IGNORECASE)


def _is_disposable(token: str) -> bool:
    """Does this token describe processing rather than biology?"""
    lowered = token.lower()
    return lowered in DISPOSABLE_TOKENS or bool(_CUTOFF_TOKEN.match(lowered))


def drop_disposable_suffix(name: str) -> str:
    """Remove trailing pipeline metadata, stopping at the first real word.

    Works right to left and stops as soon as a token is not recognisable as
    processing metadata, so the cell type at the end of the meaningful part is
    never crossed.
    """
    tokens = _split_tokens(name)
    while tokens:
        last = tokens[-1]
        if last in _SPLIT_SEPARATORS:
            tokens.pop()
            continue
        if _is_disposable(last):
            tokens.pop()
            continue
        break
    return "".join(tokens).strip(_NAME_SEPARATORS) or name


#: The literal Paralome writes into every output filename, immediately after
#: the statistical test.
#:
#: Paralome names its output
#: ``<contrast>_<celltype>_<method>_<test>_significant_<threshold>_<assay>``, as in
#: ``Estrus_vs_2dpp_Immature_cortical_ovarian_stroma_naive_cell_t_significant_p0.05_rna``.
#: Only the contrast and the cell type describe the biology; everything from the
#: method rightwards describes how the numbers were produced.
#:
#: Anchoring on this literal makes the cut exact rather than a guess, and needs
#: no list of test names -- the test is simply the token before it.
PARALOME_ANCHOR = "significant"

#: Aggregation methods Paralome puts between the cell type and the test.
#:
#: Matched as whole phrases, not as loose words, and only in the one position
#: directly before the test token. Both restrictions matter: a cell type of
#: ``Naive_T_cell`` shares words with the ``naive_cell`` method, and treating
#: those words as disposable wherever they appear would cut it to ``Naive_T``.
#:
#: Extend this, or use --strip, if Paralome grows another method.
PARALOME_METHODS = (
    ("naive", "cell"),
    ("single", "cell"),
    ("pseudobulk",),
    ("bulk",),
)


def _words(tokens: Sequence[str]) -> List[int]:
    """Indices of the real words in a token list, skipping separators."""
    return [i for i, t in enumerate(tokens) if t not in _SPLIT_SEPARATORS]


def strip_paralome_suffix(name: str) -> str:
    """Cut a Paralome filename back to the contrast and the cell type.

    Works backwards from the ``significant`` anchor: drop it and everything
    after it, drop the statistical test immediately before it, then drop the
    aggregation method if one is there.

    Returns the name unchanged if the anchor is absent -- the file did not come
    from Paralome, and the generic rules should handle it instead.
    """
    tokens = _split_tokens(name)
    words = _words(tokens)

    position = None
    for index in reversed(range(len(words))):
        if tokens[words[index]].lower() == PARALOME_ANCHOR:
            position = index
            break
    if position is None or position < 2:
        # No anchor, or nothing would be left once the test is removed.
        return name

    cut = position - 1  # the statistical test

    # At most one method, matched as a whole phrase ending where the test began.
    lowered = [tokens[i].lower() for i in words]
    for method in sorted(PARALOME_METHODS, key=len, reverse=True):
        start = cut - len(method)
        if start > 0 and tuple(lowered[start:cut]) == method:
            cut = start
            break

    return "".join(tokens[: words[cut]]).strip(_NAME_SEPARATORS) or name


def _usable(stripped: Sequence[str], original: Sequence[str]) -> bool:
    """Is this set of shortened names still worth having?

    Three things have to hold: nothing empty, every name still distinct (the
    point is telling observations apart in an IPA comparison analysis), and
    enough left to read.
    """
    if not all(stripped):
        return False
    if len(set(stripped)) != len(set(original)):
        return False
    return all(
        len(name) >= MIN_STRIPPED_NAME and any(c.isalpha() for c in name)
        for name in stripped
    )


def strip_shared_suffix(names: Sequence[str]) -> List[str]:
    """Drop the trailing tokens every name in the batch has in common."""
    _, suffix = shared_affixes(names)
    end = len("".join(suffix))
    if not end:
        return list(names)
    candidate = [name[: len(name) - end].strip(_NAME_SEPARATORS) for name in names]
    return candidate if _usable(candidate, names) else list(names)


def strip_shared_prefix(names: Sequence[str]) -> List[str]:
    """Drop the leading tokens every name has in common.

    A last resort. The front of a filename usually carries the contrast --
    ``Estrus_vs_2dpp`` -- which is one of the two things a reader needs, so this
    runs only when removing pipeline metadata has not freed up enough room.
    """
    prefix, _ = shared_affixes(names)
    start = len("".join(prefix))
    if not start:
        return list(names)
    candidate = [name[start:].strip(_NAME_SEPARATORS) for name in names]
    return candidate if _usable(candidate, names) else list(names)


def trim_name(name: str, limit: int = MAX_OBSERVATION_NAME, marker: str = "..") -> str:
    """Cut a name to *limit* characters, keeping both ends.

    The last resort, once removing pipeline metadata has not freed up enough
    room. Both ends are kept because by this point both are carrying meaning --
    the contrast at the front, the cell type at the back. The cut lands on a
    token boundary so no word is left as a fragment, and the marker makes it
    obvious something was removed rather than leaving a name that looks
    complete but is not.
    """
    name = (name or "").strip()
    if len(name) <= limit:
        return name

    budget = limit - len(marker)
    head_budget = budget * 3 // 5
    tokens = _split_tokens(name)

    head = ""
    for token in tokens:
        if len(head) + len(token) > head_budget:
            break
        head += token
    head = head.strip(_NAME_SEPARATORS)

    tail = ""
    for token in reversed(tokens):
        if len(head) + len(tail) + len(token) > budget:
            break
        tail = token + tail
    tail = tail.strip(_NAME_SEPARATORS)

    if not head and not tail:  # a single token longer than the limit; cut it
        return name[:limit]
    return f"{head}{marker}{tail}" if tail else f"{head}{marker}"


def observation_names(
    names: Sequence[str],
    limit: int = MAX_OBSERVATION_NAME,
    strip: Optional[Sequence[str]] = None,
) -> List[str]:
    """Turn filenames into observation names IPA will accept.

    Two things in a pipeline filename matter to whoever reads the analysis: the
    contrast (``Estrus_vs_2dpp``) and what it was computed from
    (``Immature_cortical_ovarian_stroma``). Everything the pipeline appends
    about how it ran is disposable. So removal is graded, least damaging first,
    and stops the moment the names fit:

    1. anything named with ``--strip``
    2. the Paralome tail, cut exactly at its ``significant`` anchor
    3. trailing pipeline metadata, for files from anything else
    4. the suffix every file in the batch shares
    5. the prefix every file shares -- this costs the contrast, so it is late
    6. a two-ended cut with ``..``, which costs part of both

    Names already within the limit are returned untouched.
    """
    names = list(names)

    # Nothing is rewritten unless it has to be -- except when --strip was given,
    # which is the user asking for a rewrite regardless of length.
    if not strip and all(len(name) <= limit for name in names):
        return names

    if strip:
        # Applied together and judged once. Validating each pattern separately
        # can accept the first and reject the second, leaving names half
        # stripped -- asymmetric, and worse than not stripping at all.
        candidate = list(names)
        for text in strip:
            candidate = [n.replace(text, "") for n in candidate]
        candidate = [
            re.sub(r"[_\-.]{2,}", "_", n).strip(_NAME_SEPARATORS) for n in candidate
        ]
        if _usable(candidate, names):
            names = candidate
        else:
            print(
                "note: --strip ignored -- applying it would leave the observation "
                "names empty, unreadable, or no longer distinct from each other.",
                file=sys.stderr,
            )

    # Pipeline metadata goes first and goes completely -- both the tokens
    # recognisable as settings and whatever tail the whole batch happens to
    # share. Doing these together matters: stopping as soon as the names merely
    # fit would leave half a suffix behind, which is worse than either whole.
    for step in (lambda ns: [strip_paralome_suffix(n) for n in ns],
                 lambda ns: [drop_disposable_suffix(n) for n in ns],
                 strip_shared_suffix):
        candidate = step(names)
        if _usable(candidate, names):
            names = candidate

    if all(len(name) <= limit for name in names):
        return names

    # Only now start taking things a reader wanted.
    candidate = strip_shared_prefix(names)
    if _usable(candidate, names):
        names = candidate
    if all(len(name) <= limit for name in names):
        return names

    return [trim_name(name, limit) for name in names]


def _report_shortened_names(requested: Sequence[str], resolved: Sequence[str]) -> None:
    """Say what was renamed and why -- once for the batch, not once per file."""
    changed = [(was, now) for was, now in zip(requested, resolved) if was != now]
    if not changed:
        return
    noun = "name" if len(changed) == 1 else "names"
    print(
        f"note: shortened {len(changed)} observation {noun}. IPA rejects a long "
        f"observation name and reports it as an outage, so this is not optional.\n"
        f"      Datasets and analyses keep the full filename; only the "
        f"observation label inside the analysis is shorter."
    )
    for was, now in changed:
        print(f"      {was}\n   -> {now}")
    print()


def _load_datasets(args) -> Tuple[List[Dataset], List[Tuple[pathlib.Path, str]]]:
    """Discover files, build the mapping for each, and validate them.

    Returns the datasets that validated and a list of ``(path, reason)`` for
    those that did not. Callers decide what to do with the failures: `validate`
    reports them all and stops, while `submit` on a directory files them into
    ``failed/`` and carries on with the rest.
    """
    paths = discover_files(args.path, getattr(args, "pattern", None), getattr(args, "recursive", False))

    if len(paths) > 1:
        for attr in _SINGLE_FILE_FLAGS:
            if getattr(args, attr, None):
                flag = "--" + attr.replace("_", "-")
                raise IPAError(
                    f"{flag} applies to a single file, but {len(paths)} files matched. "
                    "Names are taken from each filename; use --project to group them."
                )

    # Observation names are decided for the batch as a whole, because what is
    # safe to drop from one name depends on what the others contain.
    requested = [getattr(args, "observation", None) or path.stem for path in paths]
    resolved = observation_names(requested, strip=getattr(args, "strip", None))
    _report_shortened_names(requested, resolved)
    names = dict(zip(paths, resolved))

    datasets: List[Dataset] = []
    problems: List[Tuple[pathlib.Path, str]] = []
    for path in paths:
        try:
            frame = load_table(path, sep=args.sep, skip_rows=args.skip_rows)
            mapping = build_mapping(
                columns=list(frame.columns),
                id_specs=args.ID,
                fc_spec=args.FC,
                observation_name=names[path],
                pvalue_spec=getattr(args, "pvalue", None),
                fdr_spec=getattr(args, "fdr", None),
            )
            dataset = Dataset.from_frame(
                frame,
                mapping,
                name=getattr(args, "dataset_name", None) or path.stem,
                check_ranges=not args.no_range_check,
            )
            dataset.source_path = str(path.resolve())
            datasets.append(dataset)
        except IPAError as exc:
            problems.append((path, str(exc)))

    return datasets, problems


# -- shared arguments ------------------------------------------------------


def _add_mapping_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path",
        help="a delimited dataset file, or a directory to search for them",
    )
    parser.add_argument(
        "--pattern",
        default=None,
        metavar="TEXT",
        help="when PATH is a directory, only use files matching this. Plain text "
        "matches as a substring (SampleA finds SampleA_DEG.txt); text containing "
        "* ? or [ is treated as a glob. Default: "
        + ", ".join(TABLE_PATTERNS),
    )
    parser.add_argument(
        "--pvalue",
        type=parse_pvalue_spec,
        default=None,
        metavar="COLUMN[:CUTOFF]",
        help="0-based column holding p-values, with an optional cutoff. Values "
        "must lie in [0, 1]; IPA silently discards anything outside it",
    )
    parser.add_argument(
        "--fdr",
        type=parse_fdr_spec,
        default=None,
        metavar="COLUMN[:CUTOFF]",
        help="0-based column holding false discovery rates, with an optional "
        "cutoff. NOTE IPA reads this as a PERCENTAGE in [0, 100], so a q-value "
        "of 0.05 means 0.05%%, not 5%%",
    )
    parser.add_argument(
        "--strip",
        action="append",
        default=None,
        metavar="TEXT",
        help="remove TEXT from observation names before anything else. Repeatable. "
        "Use it when a pipeline's suffix is not recognised automatically, e.g. "
        "--strip _significant_p0.05_rna. Ignored if it would empty a name or make "
        "two names identical",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="search subdirectories of PATH as well",
    )
    parser.add_argument(
        "--ID",
        action="append",
        required=True,
        type=parse_id_spec,
        metavar="COLUMN:TYPE",
        help="0-based identifier column and its IPA gene ID type, e.g. 0:ensembl. "
        "IPA validates the type and names it if unrecognised. "
        "Give twice for a fallback identifier used where the primary is blank",
    )
    parser.add_argument(
        "--FC",
        required=True,
        type=parse_fc_spec,
        metavar="COLUMN:TYPE[:CUTOFF]",
        help="0-based fold-change column, its measurement type and optional "
        "cutoff, e.g. 1:foldchange:1.5",
    )
    parser.add_argument(
        "--sep",
        default=None,
        help="field delimiter; sniffed from the header line when omitted",
    )
    parser.add_argument(
        "--skip-rows",
        type=int,
        default=0,
        metavar="N",
        help="discard N lines before the header row, for files with a comment "
        "or title line above it. Column numbers still count from the header",
    )
    parser.add_argument(
        "--observation",
        default=None,
        help="observation name shown in IPA (default: the filename). Single file only",
    )
    parser.add_argument(
        "--no-range-check",
        action="store_true",
        help="skip checking that values fall in the range IPA expects for their type",
    )


def _add_auth_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-cache", action="store_true", help="ignore any cached OAuth token"
    )
    parser.add_argument(
        "--application-name",
        default="PythonAPI",
        help="applicationname IPA scopes the session to",
    )
    parser.add_argument(
        "--token-file",
        default=None,
        metavar="PATH",
        help="token cache to use instead of the default in ~/.cache/ipaapi. "
        "Point this at a cache copied from a machine that can run a browser",
    )
    parser.add_argument(
        "--browser",
        default=None,
        metavar="NAME",
        help="browser to open for login, e.g. firefox. Only used when a login "
        "is actually needed; under ssh -X this displays on your local machine",
    )


def _client(args):
    from .auth import TokenCache
    from .client import IPAClient

    cache = None
    if not args.no_cache:
        token_file = getattr(args, "token_file", None)
        cache = TokenCache(path=token_file) if token_file else TokenCache()

    return IPAClient.login(
        cache=cache,
        application_name=args.application_name,
        browser=getattr(args, "browser", None),
    )


# -- subcommands -----------------------------------------------------------


def cmd_login(args) -> int:
    """Authenticate and cache a token, without submitting anything.

    Every other command logs in as a side effect of doing something else, which
    makes authentication awkward to test: the only way to find out whether
    credentials work is to spend analysis allowance finding out. This does the
    login on its own, and says where the token went and how long it lasts.
    """
    # The default client and host are used deliberately rather than being
    # exposed as flags: the cache is keyed on (client_id, application_name,
    # host), so a login under a different key would be invisible to every other
    # command -- a login that appears to work and changes nothing.
    from .auth import DEFAULT_CLIENT_ID, DEFAULT_HOST, TokenCache, login

    cache = None
    if not args.no_cache:
        cache = TokenCache(path=args.token_file) if args.token_file else TokenCache()

    if args.forget:
        if cache is None:
            raise IPAError("--forget needs the token cache; drop --no-cache.")
        cache.clear()
        print(f"Cleared the token cache at {cache.path}.")
        return 0

    existing = None
    if cache is not None and not args.force:
        existing = cache.get(
            client_id=DEFAULT_CLIENT_ID,
            application_name=args.application_name,
            host=DEFAULT_HOST,
            allow_expired=True,
        )

    if existing is not None and not existing.is_expired and not args.force:
        print(f"Already signed in as {args.application_name} on {DEFAULT_HOST}.")
        print(f"  token cache: {cache.path}")
        print(f"  {_expiry_phrase(existing)}")
        print("\nUse --force to sign in again anyway.")
        return 0

    credentials = login(
        client_id=DEFAULT_CLIENT_ID,
        application_name=args.application_name,
        host=DEFAULT_HOST,
        cache=cache,
        open_browser=not args.no_browser,
        browser=getattr(args, "browser", None),
        force=args.force,
    )

    print("Signed in.")
    if cache is not None:
        print(f"  token cache: {cache.path}")
    print(f"  {_expiry_phrase(credentials)}")
    if credentials.refresh_token:
        print(
            "  a refresh token was issued, so the next command should not need "
            "the browser"
        )
    else:
        print(
            "  no refresh token was issued -- expect to sign in again when this "
            "one expires"
        )
    if cache is not None:
        print(
            f"\nTo use this token on another machine, copy {cache.path} across and "
            "point IPAAPI_TOKEN_FILE at it, or pass --token-file."
        )
    return 0


def _expiry_phrase(credentials) -> str:
    """Describe when a token runs out, in terms worth acting on."""
    import time

    if credentials.expires_at is None:
        return "expiry: not reported by IPA"
    remaining = credentials.expires_at - time.time()
    if remaining <= 0:
        return "expiry: already expired"
    hours, minutes = divmod(int(remaining) // 60, 60)
    span = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(credentials.expires_at))
    return f"expires in {span} (at {stamp})"


def cmd_validate(args) -> int:
    """Check the mapping against the file(s) without contacting IPA."""
    datasets, problems = _load_datasets(args)
    if problems:
        raise IPAError(
            f"{len(problems)} of {len(datasets) + len(problems)} file(s) do not fit "
            "the mapping:\n  - "
            + "\n  - ".join(f"{path.name}: {reason}" for path, reason in problems)
        )
    for index, dataset in enumerate(datasets):
        if index:
            print()
        print(dataset.describe())
        if len(datasets) == 1:
            print()
            print(dataset.preview())
    noun = "file" if len(datasets) == 1 else "files"
    print(f"\n{len(datasets)} {noun} valid. Nothing was uploaded.")
    return 0


def cmd_submit(args) -> int:
    """Upload the dataset(s) into a project and start the analyses."""
    root = pathlib.Path(args.path).expanduser()
    directory_mode = root.is_dir()
    datasets, problems = _load_datasets(args)

    # Filing only applies to a directory of files. A single named file is left
    # exactly where the user put it.
    triage = Triage(root, dry_run=args.dry_run) if directory_mode else None

    if problems and triage is None:
        raise IPAError(problems[0][1])

    # If every file fails the same way, the mapping is wrong, not the data.
    # Quarantining the whole directory for a command-line mistake just means
    # fishing it all back out again.
    systemic = bool(problems) and not datasets
    for path, reason in problems:
        print(f"failed validation {path.name}: {reason}", file=sys.stderr)
        if not systemic:
            triage.mark_failed(path, f"Validation failed.\n\n{reason}")

    if systemic:
        print(
            f"\nAll {len(problems)} file(s) failed validation the same way, so this "
            "looks like the mapping rather than the data.\nNothing was moved. Check "
            "--ID, --FC and --skip-rows, then re-run the same command.",
            file=sys.stderr,
        )
        return 1

    if not datasets:
        print("\nNo files left to submit.", file=sys.stderr)
        if triage is not None and triage.summary():
            print(triage.summary(), file=sys.stderr)
        return 1

    for index, dataset in enumerate(datasets):
        if index:
            print()
        print(dataset.describe())

    if args.dry_run:
        noun = "file" if len(datasets) == 1 else "files"
        print(f"\nDry run: {len(datasets)} {noun} valid; stopping before login.")
        if triage is not None and triage.summary():
            print(triage.summary())
        return 0

    client = _client(args)

    analysis_ids: List[str] = []
    failures: List[str] = []
    records: List[history.SubmissionRecord] = []
    skipped: List[str] = []
    quota_reached = False
    malformed = False
    service_down = False

    for position, dataset in enumerate(datasets):
        source = pathlib.Path(dataset.source_path) if dataset.source_path else None

        prior = (
            None
            if args.force
            else _already_submitted(args.project, dataset.name or "", args.log_file)
        )
        if prior is not None:
            print(
                f"skipping {dataset.name}: already submitted to {args.project!r} on "
                f"{prior.get('timestamp', 'an earlier run')} as analysis "
                f"{prior.get('analysis_id', '?')}. IPA would reject a second dataset "
                "of the same name. Use --force to submit it again anyway."
            )
            skipped.append(dataset.name or "")
            if triage is not None and source is not None:
                triage.mark_submitted(source)
            continue

        try:
            submitted = client.submit(
                dataset,
                project=args.project,
                analysis_name=args.analysis_name,
                dataset_name=args.dataset_name,
                reference_set=(
                    None if args.reference_set == "omit" else args.reference_set
                ),
            )
        except MalformedRequestError as exc:
            # The command line is wrong, not the data. Touch nothing.
            print(f"\n{exc}\n", file=sys.stderr)
            if triage is not None:
                for remaining in datasets[position:]:
                    if remaining.source_path:
                        triage.mark_left(pathlib.Path(remaining.source_path))
            failures.append(f"{dataset.name}: malformed request")
            malformed = True
            break
        except (QuotaExceededError, AnalysisRefusedError, ServiceUnavailableError) as exc:
            # The file is fine; IPA will not run it right now. Leave this one
            # and everything after it for the next run.
            quota_reached = True
            service_down = isinstance(exc, ServiceUnavailableError)
            if isinstance(exc, QuotaExceededError):
                label = "Allowance exhausted"
            elif isinstance(exc, ServiceUnavailableError):
                label = "IPA is unavailable"
            else:
                label = "IPA declined to start the analysis"
            print(f"\n{label} while submitting {dataset.name}:\n{exc}",
                  file=sys.stderr)
            if triage is not None:
                for remaining in datasets[position:]:
                    if remaining.source_path:
                        triage.mark_left(pathlib.Path(remaining.source_path))
            break
        except IPAError as exc:
            failures.append(f"{dataset.name}: {exc}")
            print(f"FAILED {dataset.name}: {exc}", file=sys.stderr)
            if triage is not None and source is not None:
                triage.mark_failed(source, f"Submission rejected by IPA.\n\n{exc}")
            continue

        analysis_ids.extend(submitted)
        print(f"submitted {dataset.name}: {', '.join(submitted)}")

        # One record per analysis, so an ID is never only in the scrollback.
        observations = [obs.name for obs in dataset.mapping.observations]
        records.extend(
            history.SubmissionRecord(
                analysis_id=analysis_id,
                project=args.project,
                dataset_name=dataset.name or "",
                observation=observations[i] if i < len(observations) else "",
                source_file=dataset.source_path or "",
                application_name=client.application_name,
                host=client.host,
            )
            for i, analysis_id in enumerate(submitted)
        )
        if triage is not None and source is not None:
            triage.mark_submitted(source)

    log_path = history.append(records, path=args.log_file)

    if triage is not None and triage.summary():
        print("\n" + triage.summary())
    if quota_reached:
        print(
            "Re-run the same command later; the files left in place are exactly "
            "the ones still to do."
        )
        if service_down:
            print("Nothing about your command needs changing.")
    if malformed:
        # Only claim nothing moved when nothing did -- earlier files in the
        # batch may well have been submitted and filed before this one failed.
        if triage is not None and (triage.submitted or triage.failed):
            print(
                "Files submitted before the failure have been filed; the rest "
                "were left in place. Fix the parameter and re-run the same "
                "command.",
                file=sys.stderr,
            )
        else:
            print(
                "No files were moved. Fix the parameter and re-run the same "
                "command.",
                file=sys.stderr,
            )

    if skipped:
        print(
            f"\n{len(skipped)} file(s) were already in {args.project!r} and were "
            "skipped rather than resubmitted."
        )

    if not analysis_ids:
        if skipped and not failures and not quota_reached:
            print(
                f"\nNothing new to submit -- all {len(skipped)} file(s) are already "
                f"in {args.project!r}."
            )
            return 0
        print("\nNothing was submitted successfully.", file=sys.stderr)
        return 1

    noun = "analysis" if len(analysis_ids) == 1 else "analyses"
    print(f"\nSubmitted {len(analysis_ids)} {noun}.")
    if failures:
        print(f"{len(failures)} of {len(datasets)} file(s) failed to submit.", file=sys.stderr)

    if not args.wait:
        joined = " ".join(analysis_ids)
        print(
            "Analyses are running in IPA. Check on them with:\n"
            f"  ipaapi status {joined}\n"
            f"  ipaapi report {joined}\n"
            "Or re-run with --wait to block until they finish."
        )
        if log_path:
            print(f"Recorded in {log_path} -- see 'ipaapi history'.")
        return 1 if (failures or quota_reached) else 0

    statuses = client.wait_for(analysis_ids, interval=args.interval, timeout=args.timeout)
    exit_code = 1 if (failures or quota_reached) else 0
    for analysis_id, status in statuses.items():
        print(f"{analysis_id}: {status.name.lower()}")
        if status.succeeded:
            try:
                print(f"  {client.report_url(analysis_id)}")
            except IPAError as exc:
                print(f"  no report link: {exc}")
        else:
            exit_code = 1
    return exit_code


def cmd_status(args) -> int:
    """Report the current status of one or more analyses."""
    client = _client(args)
    exit_code = 0
    for analysis_id in args.analysis_ids:
        status = client.status(analysis_id)
        print(f"{analysis_id}: {status.name.lower()}")
        if not status.succeeded:
            exit_code = 1
    return exit_code


def cmd_report(args) -> int:
    """Print (and optionally open) IPA Interpret links."""
    client = _client(args)
    exit_code = 0
    for analysis_id in args.analysis_ids:
        # An unfinished analysis has no Interpret link yet and the endpoint
        # answers with a bare HTTP 500, so say what is actually going on.
        status = client.status(analysis_id)
        if not status.is_terminal:
            print(f"{analysis_id}: still running -- the link exists once it finishes")
            exit_code = 1
            continue
        if not status.succeeded:
            print(f"{analysis_id}: {status.name.lower()} -- no report for this analysis")
            exit_code = 1
            continue
        try:
            url = client.report_url(analysis_id)
        except IPAError as exc:
            print(f"{analysis_id}: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print(f"{analysis_id}: {url}")
        if args.open:
            import webbrowser

            webbrowser.open(url)
    return exit_code


def cmd_history(args) -> int:
    """List analyses submitted through this tool, oldest first."""
    rows = history.read(args.log_file)

    if args.project:
        rows = [r for r in rows if r.get("project") == args.project]
    if args.since:
        rows = [r for r in rows if r.get("timestamp", "") >= args.since]
    if args.limit:
        rows = rows[-args.limit :]

    if not rows:
        print(
            "No submissions recorded"
            + (f" in {args.log_file}" if args.log_file else "")
            + ". The log only covers analyses submitted through this tool."
        )
        return 0

    client = _client(args) if args.status else None

    widths = {
        "timestamp": max(len(r.get("timestamp", "")) for r in rows),
        "analysis_id": max(len(r.get("analysis_id", "")) for r in rows),
        "project": max(len(r.get("project", "")) for r in rows),
        "dataset_name": max(len(r.get("dataset_name", "")) for r in rows),
    }
    for row in rows:
        line = "  ".join(
            [
                row.get("timestamp", "").ljust(widths["timestamp"]),
                row.get("analysis_id", "").ljust(widths["analysis_id"]),
                row.get("project", "").ljust(widths["project"]),
                row.get("dataset_name", "").ljust(widths["dataset_name"]),
            ]
        )
        if client is not None:
            try:
                line += "  " + client.status(row["analysis_id"]).name.lower()
            except IPAError as exc:
                line += f"  (status unavailable: {exc})"
        print(line)

    ids = " ".join(r.get("analysis_id", "") for r in rows)
    print(f"\n{len(rows)} submission(s). Report links: ipaapi report {ids}")
    return 0


# -- parser ----------------------------------------------------------------


class _ListIdTypes(argparse.Action):
    """Print every documented gene ID type and exit."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        width = max(len(t) for t in GENE_ID_TYPES)
        print("gene ID types accepted by IPA (Integration Module, April 2026 s3.1):\n")
        for value, database in GENE_ID_TYPES.items():
            print(f"  {value.ljust(width)}  {database}")
        print(
            "\nSpecies is carried by the identifier type -- there is no species "
            "parameter.\nSeveral values are aliases: hugo / humansymeg / humanegsym "
            "are the same thing."
        )
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipaapi",
        description="Submit datasets to QIAGEN Ingenuity Pathway Analysis.",
        epilog=_EPILOG,
        formatter_class=_Formatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version_banner(),
        help="show the version, and which installation is being run",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    validate = subparsers.add_parser(
        "validate",
        help="check the mapping against a file without uploading",
        description=cmd_validate.__doc__,
        epilog=_EPILOG,
        formatter_class=_Formatter,
    )
    _add_mapping_arguments(validate)
    validate.set_defaults(func=cmd_validate)
    validate.add_argument(
        "--list-id-types", action=_ListIdTypes, help="list every gene ID type and exit"
    )

    submit = subparsers.add_parser(
        "submit",
        help="upload a dataset into a project and start the analysis",
        description=cmd_submit.__doc__,
        epilog=_EPILOG,
        formatter_class=_Formatter,
    )
    _add_mapping_arguments(submit)
    _add_auth_arguments(submit)
    submit.add_argument(
        "--list-id-types", action=_ListIdTypes, help="list every gene ID type and exit"
    )
    submit.add_argument("--project", required=True, help="destination IPA project")
    submit.add_argument("--analysis-name", default=None, help="override analysis name")
    submit.add_argument("--dataset-name", default=None, help="override dataset name")
    submit.add_argument(
        "--reference-set",
        default="omit",
        choices=[r.value for r in ReferenceSet] + ["omit"],
        help="background the analysis is scored against. 'ipkb' is the Ingenuity "
        "Knowledge Base; 'dataset' is the genes you uploaded. 'omit' (default) "
        "lets IPA choose. The docs say it picks by size (ipkb under 2000 "
        "identifiers, dataset at 2000+) but that has not been observed to hold, "
        "so set it explicitly for anything you will compare against itself",
    )
    submit.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="submission log to append to (default: "
        "~/.local/state/ipaapi/submissions.tsv)",
    )
    submit.add_argument(
        "--force",
        action="store_true",
        help="submit even if the log shows this dataset already went to this "
        "project. IPA rejects a duplicate dataset name, so this normally fails",
    )
    submit.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and stop before logging in or uploading",
    )
    submit.add_argument(
        "--wait",
        action="store_true",
        help="poll until the analyses finish and print their report links, "
        "instead of returning as soon as they are queued",
    )
    # Accepted silently: --no-wait is now the default, so old commands still run.
    submit.add_argument("--no-wait", action="store_true", help=argparse.SUPPRESS)
    submit.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="seconds between status polls; only used with --wait",
    )
    submit.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="seconds to wait for completion; only used with --wait",
    )
    submit.set_defaults(func=cmd_submit)

    status = subparsers.add_parser(
        "status",
        help="check the status of existing analyses",
        description=cmd_status.__doc__,
        formatter_class=_Formatter,
    )
    status.add_argument("analysis_ids", nargs="+", metavar="ANALYSIS_ID")
    _add_auth_arguments(status)
    status.set_defaults(func=cmd_status)

    report = subparsers.add_parser(
        "report",
        help="print IPA Interpret links for analyses",
        description=cmd_report.__doc__,
        formatter_class=_Formatter,
    )
    report.add_argument("analysis_ids", nargs="+", metavar="ANALYSIS_ID")
    report.add_argument("--open", action="store_true", help="also open them in a browser")
    _add_auth_arguments(report)
    report.set_defaults(func=cmd_report)

    hist = subparsers.add_parser(
        "history",
        help="list analyses submitted through this tool",
        description=cmd_history.__doc__,
        epilog=(
            "IPA's API cannot list the analyses on an account, so this package "
            "keeps its own log. It covers submissions made through this tool "
            "only -- analyses submitted from the IPA client will not appear.\n\n"
            "examples:\n"
            "  ipaapi history\n"
            "  ipaapi history --project singlet_RNA_P05\n"
            "  ipaapi history --since 2026-08-01 --status\n"
        ),
        formatter_class=_Formatter,
    )
    hist.add_argument("--project", default=None, help="only this project")
    hist.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help="only submissions on or after this date",
    )
    hist.add_argument(
        "--limit", type=int, default=None, metavar="N", help="only the last N entries"
    )
    hist.add_argument(
        "--status",
        action="store_true",
        help="look up the current status of each analysis (requires login)",
    )
    hist.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="submission log to read (default: ~/.local/state/ipaapi/submissions.tsv)",
    )
    _add_auth_arguments(hist)
    hist.set_defaults(func=cmd_history)

    signin = subparsers.add_parser(
        "login",
        help="authenticate and cache a token without submitting anything",
        description=cmd_login.__doc__,
        epilog=(
            "Useful for three things: checking that credentials work without "
            "spending analysis allowance, refreshing a token before a long "
            "batch so it does not expire mid-run, and setting up a headless "
            "server.\n\n"
            "Headless: run this on a machine with a browser, then copy the "
            "token cache to the server and point IPAAPI_TOKEN_FILE at it. Or "
            "forward the browser with 'ssh -X' and run it there.\n\n"
            "examples:\n"
            "  ipaapi login\n"
            "  ipaapi login --force              # sign in again even if valid\n"
            "  ipaapi login --no-browser         # print the URL instead\n"
            "  ipaapi login --forget             # clear the cached token\n"
        ),
        formatter_class=_Formatter,
    )
    signin.add_argument(
        "--force",
        action="store_true",
        help="sign in again even if a valid token is cached",
    )
    signin.add_argument(
        "--forget",
        action="store_true",
        help="delete the cached token and exit",
    )
    signin.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorization URL instead of opening a browser",
    )
    _add_auth_arguments(signin)
    signin.set_defaults(func=cmd_login)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``ipaapi`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except IPAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
