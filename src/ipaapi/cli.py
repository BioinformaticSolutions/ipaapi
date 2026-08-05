"""Command-line interface for :mod:`ipaapi`.

Installed as the ``ipaapi`` console script::

    ipaapi --help
    ipaapi submit data.txt --ID 0:ensembl --FC 1:foldchange --project MyProject

Column positions are **0-based**: ``--ID 0`` is the first column in the file.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence, Tuple

from . import __version__
from .dataset import Dataset, load_table
from .errors import IPAError
from .mapping import ColumnMapping, Measurement, Observation
from .models import MeasurementType, ReferenceSet

__all__ = ["main"]

# Common IPA gene identifier types. IPA is the authority on what it accepts, so
# these are offered as guidance rather than enforced -- an unrecognised type is
# passed through and validated server-side.
COMMON_ID_TYPES = (
    "ensembl",
    "entrezgene",
    "genesymbol",
    "refseq",
    "affymetrix",
    "agilent",
    "illumina",
    "uniprot",
    "unigene",
)

_EPILOG = f"""\
column positions are 0-based: --ID 0 is the first column in the file

--ID may be given up to twice. The first is the primary identifier and is what
IPA is told the gene ID type is. The second, if present, is used only for rows
where the primary is blank. Because IPA accepts one gene ID type per submission,
rows filled from a second identifier of a different type are uploaded under the
primary's type and may not map; the fill count is always reported.

common ID types: {', '.join(COMMON_ID_TYPES)}
measurement types for --FC: ratio, foldchange, logratio

examples:
  ipaapi validate rnaseq.txt --ID 0:ensembl --FC 1:foldchange
  ipaapi submit rnaseq.txt --ID 0:ensembl --FC 1:foldchange:1.5 --project Study1
  ipaapi submit rnaseq.txt --ID 0:ensembl --ID 4:genesymbol \\
      --FC 1:foldchange --project Study1
  ipaapi status abc-123 abc-124
  ipaapi report abc-123 --open
"""


class _Formatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter
):
    """Show defaults, but leave the epilog's line breaks alone."""


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
            f"--ID {text!r} is missing the identifier type. "
            f"Common types: {', '.join(COMMON_ID_TYPES)}."
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


def _load(args) -> Dataset:
    """Read the file, build the mapping from the specs, and validate."""
    frame = load_table(args.dataset, sep=args.sep)
    mapping = build_mapping(
        columns=list(frame.columns),
        id_specs=args.ID,
        fc_spec=args.FC,
        observation_name=getattr(args, "observation", None),
    )
    return Dataset.from_frame(
        frame,
        mapping,
        name=getattr(args, "dataset_name", None) or _stem(args.dataset),
        check_ranges=not args.no_range_check,
    )


def _stem(path: str) -> str:
    import os

    return os.path.splitext(os.path.basename(str(path)))[0]


# -- shared arguments ------------------------------------------------------


def _add_mapping_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dataset", help="path to the delimited dataset file")
    parser.add_argument(
        "--ID",
        action="append",
        required=True,
        type=parse_id_spec,
        metavar="COLUMN:TYPE",
        help="0-based identifier column and its IPA gene ID type, e.g. 0:ensembl. "
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
        help="field delimiter; sniffed from the first line when omitted",
    )
    parser.add_argument(
        "--observation",
        default=None,
        help="observation name shown in IPA (default: the fold-change column header)",
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


def _client(args):
    from .auth import TokenCache
    from .client import IPAClient

    return IPAClient.login(
        cache=None if args.no_cache else TokenCache(),
        application_name=args.application_name,
    )


# -- subcommands -----------------------------------------------------------


def cmd_validate(args) -> int:
    """Check the mapping against the file without contacting IPA."""
    dataset = _load(args)
    print(dataset.describe())
    print()
    print(dataset.preview())
    print("\nMapping is valid. Nothing was uploaded.")
    return 0


def cmd_submit(args) -> int:
    """Upload the dataset into a project and start the analysis."""
    dataset = _load(args)
    print(dataset.describe())

    if args.dry_run:
        print("\nDry run: mapping is valid; stopping before login.")
        return 0

    client = _client(args)
    analysis_ids = client.submit(
        dataset,
        project=args.project,
        analysis_name=args.analysis_name,
        reference_set=args.reference_set,
    )
    print(f"\nSubmitted: {', '.join(analysis_ids)}")

    if args.no_wait:
        print("Not waiting. Check progress with: ipaapi status " + " ".join(analysis_ids))
        return 0

    statuses = client.wait_for(analysis_ids, interval=args.interval, timeout=args.timeout)
    exit_code = 0
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
    for analysis_id, url in client.report_urls(args.analysis_ids).items():
        if url is None:
            exit_code = 1
            continue
        print(f"{analysis_id}: {url}")
        if args.open:
            import webbrowser

            webbrowser.open(url)
    return exit_code


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipaapi",
        description="Submit datasets to QIAGEN Ingenuity Pathway Analysis.",
        epilog=_EPILOG,
        formatter_class=_Formatter,
    )
    parser.add_argument("--version", action="version", version=f"ipaapi {__version__}")
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
        choices=[r.value for r in ReferenceSet],
        help="background set the analysis is scored against",
    )
    submit.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and stop before logging in or uploading",
    )
    submit.add_argument(
        "--no-wait", action="store_true", help="return as soon as the analysis is queued"
    )
    submit.add_argument(
        "--interval", type=float, default=30.0, help="seconds between status polls"
    )
    submit.add_argument(
        "--timeout", type=float, default=3600.0, help="seconds to wait for completion"
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
