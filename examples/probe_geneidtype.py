"""Find which gene ID type strings IPA accepts.

IPA's accepted ``geneidtype`` vocabulary is not documented publicly, and is
narrower than the obvious names suggest: ``ensembl`` works, ``genesymbol`` is
rejected with "Unknown GeneId Type (genesymbol)". Since IPA names the value it
rejected, candidates can simply be tried.

    python3 probe_geneidtype.py --column Common_name --file mydata.csv

**A type IPA accepts creates a real analysis and consumes allowance.** The probe
therefore stops at the first success, and by default submits a two-gene extract
rather than your whole file. Rejected types cost nothing -- they fail before any
analysis is created.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from ipaapi import ColumnMapping, Dataset, Measurement, MeasurementType, Observation
from ipaapi.auth import TokenCache
from ipaapi.client import IPAClient
from ipaapi.errors import MalformedRequestError, SubmissionError

# Ordered by the IPA desktop client's label for a gene symbol column:
# "Gene Symbol - human (HUGO / HGNC / Entrez Gene)". The client and the REST
# API need not share vocabulary, so these are still guesses -- but drawn from
# IPA's own wording. 'genesymbol' is already known to be rejected.
DEFAULT_CANDIDATES = (
    "hgnc",
    "entrezgene",
    "refseq",
    "uniprot",
    "genbank",
    "mirbase",
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="a real data file to take rows from")
    parser.add_argument("--column", required=True, help="the identifier column to test")
    parser.add_argument("--fc-column", required=True, help="a fold-change column")
    parser.add_argument("--skip-rows", type=int, default=1)
    parser.add_argument("--rows", type=int, default=2, help="rows to submit per attempt")
    parser.add_argument(
        "--project", default="ipaapi_geneidtype_probe", help="throwaway project"
    )
    parser.add_argument(
        "--candidates",
        nargs="+",
        default=list(DEFAULT_CANDIDATES),
        help="type strings to try, in order",
    )
    args = parser.parse_args(argv)

    frame = pd.read_csv(args.file, skiprows=args.skip_rows, dtype=object)
    frame = frame[frame[args.column].notna()].head(args.rows)
    if frame.empty:
        print(f"No usable rows in {args.column!r}.", file=sys.stderr)
        return 1

    print(f"Probing with {len(frame)} row(s) from {args.file}")
    print(f"identifier column {args.column!r}, values: {list(frame[args.column])}")
    print("A type that is ACCEPTED creates a real analysis. Stopping at the first.\n")

    client = IPAClient.login(cache=TokenCache())

    for candidate in args.candidates:
        mapping = ColumnMapping(
            gene_id_column=args.column,
            gene_id_type=candidate,
            observations=[
                Observation(
                    "probe",
                    [Measurement(args.fc_column, MeasurementType.LOG_RATIO)],
                )
            ],
        )
        dataset = Dataset.from_frame(
            frame, mapping, name=f"probe_{candidate}", check_ranges=False
        )
        try:
            ids = client.submit(dataset, project=args.project, reference_set=None)
        except MalformedRequestError as exc:
            first = str(exc).strip().splitlines()[-1]
            print(f"  {candidate:<12} rejected  ({first[:120]})")
            continue
        except SubmissionError as exc:
            print(f"  {candidate:<12} error     ({str(exc)[:120]})")
            continue

        print(f"\n  {candidate:<12} ACCEPTED -> analysis {', '.join(ids)}")
        print(f"\nUse --ID <column>:{candidate}")
        print(f"The probe analysis is in project {args.project!r}; delete it in IPA.")
        return 0

    print("\nNone of the candidates were accepted. Ask QIAGEN support for the")
    print("accepted geneidtype vocabulary: AdvancedGenomicsSupport@qiagen.com")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
