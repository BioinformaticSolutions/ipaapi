"""End-to-end example: map columns, upload into a project, submit, track.

Equivalent to the original demo's ``main.py``, rewritten against ``ipaapi``.

Run with a dataset path::

    python examples/quickstart.py "Data/Gemfibrozil vs Ctrl RNAseq.txt"
"""

from __future__ import annotations

import argparse
import sys

from ipaapi import (
    ColumnMapping,
    Dataset,
    IPAClient,
    Measurement,
    MeasurementType,
    Observation,
    TokenCache,
)
from ipaapi.errors import IPAError, ResultsUnavailableError


def build_mapping() -> ColumnMapping:
    """Describe the demo dataset's columns.

    Edit this to match your own file. Columns not named here are ignored, and
    the order they are declared in does not matter.
    """
    return ColumnMapping(
        gene_id_column="Gene IDs",
        gene_id_type="ensembl",
        observations=[
            Observation(
                "Gemfib vs ctrl",
                [
                    Measurement("FoldChange", MeasurementType.FOLD_CHANGE, cutoff=1.5),
                    Measurement("PValue", MeasurementType.P_VALUE),
                    Measurement(
                        "AdjustedPValue", MeasurementType.FALSE_DISCOVERY, cutoff=0.01
                    ),
                    Measurement("Group Max Intensity", MeasurementType.INTENSITY),
                ],
            ),
        ],
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Path to the delimited dataset file")
    parser.add_argument("--project", default="PythonAPI_Demo", help="IPA project name")
    parser.add_argument("--analysis-name", default=None, help="Override analysis name")
    parser.add_argument(
        "--no-cache", action="store_true", help="Ignore any cached OAuth token"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the mapping and stop before logging in",
    )
    parser.add_argument(
        "--timeout", type=float, default=3600.0, help="Seconds to wait for analyses"
    )
    args = parser.parse_args(argv)

    # 1. Load and validate. Nothing leaves the machine until this passes.
    try:
        dataset = Dataset.from_file(args.dataset, build_mapping())
    except IPAError as exc:
        print(f"Mapping problem:\n{exc}", file=sys.stderr)
        return 1

    print(dataset.describe())
    print()
    print(dataset.preview())

    if args.dry_run:
        print("\nDry run: mapping is valid; stopping before login.")
        return 0

    # 2. Authenticate. Opens a browser; cached tokens skip it on repeat runs.
    client = IPAClient.login(cache=None if args.no_cache else TokenCache())

    # 3. Upload into the project and launch one analysis per observation.
    analysis_ids = client.submit(
        dataset, project=args.project, analysis_name=args.analysis_name
    )
    print(f"\nSubmitted {len(analysis_ids)} analysis/analyses: {', '.join(analysis_ids)}")

    # 4. Poll to completion.
    statuses = client.wait_for(analysis_ids, timeout=args.timeout)

    # 5. Report. Result retrieval needs the commercial add-on; report URLs do not.
    for analysis_id, status in statuses.items():
        print(f"\n=== {analysis_id}: {status.name.lower()} ===")
        if not status.succeeded:
            continue
        try:
            print(client.report_url(analysis_id))
        except IPAError as exc:
            print(f"  no report link: {exc}")
        try:
            results = client.results(analysis_id)
            print(results.summary())
            print(results.canonical_pathways.head())
        except ResultsUnavailableError as exc:
            print(f"  results not retrieved: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
