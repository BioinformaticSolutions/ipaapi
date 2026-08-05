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
import sys
from typing import List, Optional, Sequence, Tuple

from . import __version__, history
from .dataset import Dataset, load_table
from .errors import IPAError, MalformedRequestError, QuotaExceededError
from .mapping import ColumnMapping, Measurement, Observation
from .models import MeasurementType, ReferenceSet
from .triage import TRIAGE_DIRNAMES, Triage

__all__ = ["main"]

#: Extensions searched in a directory when no --pattern is given.
TABLE_PATTERNS = ("*.txt", "*.tsv", "*.csv")

_GLOB_CHARS = set("*?[")

#: Gene identifier types confirmed to be accepted by IPA.
#:
#: Only values actually observed to work belong here. IPA's accepted vocabulary
#: is not documented publicly and is narrower than the obvious names suggest --
#: 'genesymbol', for instance, is rejected with "Unknown GeneId Type". Listing
#: plausible-looking guesses here previously sent users straight into a failed
#: submission, so the list stays empirical.
CONFIRMED_ID_TYPES = ("ensembl",)

#: Names worth trying, unverified. IPA validates server-side and names the value
#: it rejected, so an unknown type fails fast and informatively.
CANDIDATE_ID_TYPES = (
    "entrezgene",
    "symbol",
    "genename",
    "hgnc",
    "refseq",
    "uniprot",
    "affymetrix",
    "illumina",
    "agilent",
    "unigene",
)

_EPILOG = f"""\
column positions are 0-based: --ID 0 is the first column in the file

--ID may be given up to twice. The first is the primary identifier and is what
IPA is told the gene ID type is. The second, if present, is used only for rows
where the primary is blank. Because IPA accepts one gene ID type per submission,
rows filled from a second identifier of a different type are uploaded under the
primary's type and may not map; the fill count is always reported.

gene ID types confirmed to work: {', '.join(CONFIRMED_ID_TYPES)}
IPA's accepted vocabulary is undocumented and narrower than it looks --
'genesymbol' is rejected. An unknown type fails fast and IPA names it.
measurement types for --FC: ratio, foldchange, logratio

PATH may be a single file or a directory. Given a directory, --pattern selects
which files to use and each matched file is submitted as its own dataset and
analysis, named after the file. Every file must fit the same --ID/--FC layout;
all of them are validated before any is uploaded.

examples:
  ipaapi validate rnaseq.txt --ID 0:ensembl --FC 1:foldchange
  ipaapi submit rnaseq.txt --ID 0:ensembl --FC 1:foldchange:1.5 --project Study1
  ipaapi submit rnaseq.txt --ID 0:ensembl --ID 4:genesymbol \\
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
            f"{text.rstrip(':')}:{CONFIRMED_ID_TYPES[0]}."
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
) -> ColumnMapping:
    """Turn parsed CLI specs into a :class:`ColumnMapping`."""
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

    return ColumnMapping(
        gene_id_column=primary_column,
        gene_id_type=primary_type,
        gene_id_fallback_column=fallback_column,
        gene_id_fallback_type=fallback_type,
        observations=[
            Observation(
                name=observation_name or fc_column,
                measurements=[Measurement(fc_column, fc_type, cutoff=fc_cutoff)],
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

    datasets: List[Dataset] = []
    problems: List[Tuple[pathlib.Path, str]] = []
    for path in paths:
        try:
            frame = load_table(path, sep=args.sep, skip_rows=args.skip_rows)
            mapping = build_mapping(
                columns=list(frame.columns),
                id_specs=args.ID,
                fc_spec=args.FC,
                observation_name=getattr(args, "observation", None) or path.stem,
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
    quota_reached = False
    malformed = False

    for position, dataset in enumerate(datasets):
        source = pathlib.Path(dataset.source_path) if dataset.source_path else None
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
        except QuotaExceededError as exc:
            # The file is fine; the account is out of allowance. Leave this one
            # and everything after it for the next run.
            quota_reached = True
            print(f"\nAllowance exhausted while submitting {dataset.name}:\n{exc}",
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
            "Re-run the same command once the allowance resets; the files left "
            "in place are exactly the ones still to do."
        )
    if malformed:
        print(
            "No files were moved. Fix the parameter and re-run the same command.",
            file=sys.stderr,
        )

    if not analysis_ids:
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

    submit = subparsers.add_parser(
        "submit",
        help="upload a dataset into a project and start the analysis",
        description=cmd_submit.__doc__,
        epilog=_EPILOG,
        formatter_class=_Formatter,
    )
    _add_mapping_arguments(submit)
    _add_auth_arguments(submit)
    submit.add_argument("--project", required=True, help="destination IPA project")
    submit.add_argument("--analysis-name", default=None, help="override analysis name")
    submit.add_argument("--dataset-name", default=None, help="override dataset name")
    submit.add_argument(
        "--reference-set",
        default=ReferenceSet.DATASET.value,
        choices=[r.value for r in ReferenceSet] + ["omit"],
        help="background the analysis is scored against. 'dataset' uses the "
        "uploaded genes, which is right for a full transcriptome but degenerates "
        "the statistics for a pre-filtered hit list; 'omit' leaves the parameter "
        "out so IPA applies its own default",
    )
    submit.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="submission log to append to (default: "
        "~/.local/state/ipaapi/submissions.tsv)",
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
