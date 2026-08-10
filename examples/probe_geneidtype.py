"""Discover which gene ID type strings IPA accepts.

.. note::
   **Superseded.** The accepted vocabulary is documented in the IPA
   Integration Module (April 2026), section 3.1, and is available as
   ``ipaapi.models.GENE_ID_TYPES`` or ``ipaapi submit --list-id-types``.
   This script is kept for verifying the list against a live account, or
   for checking a value the documentation does not cover.

IPA's accepted ``geneidtype`` vocabulary is not documented and is not
guessable: ``ensembl`` and ``hugo`` work, while ``genesymbol`` and the desktop
client's own label ``Gene Symbol`` are both rejected. There is no endpoint that
enumerates it. What IPA does do is name the value it rejected, so candidates
can be tried one at a time.

**Cost.** Ordinarily this is not free: a type IPA *accepts* creates a real
analysis and consumes allowance. But the two failure modes are distinguishable
and one of them is free:

===========================  ==========================================
Response                     Meaning
===========================  ==========================================
``Unknown GeneId Type (X)``  rejected before any analysis was created
``Analysis limit exceeded``  **accepted** -- it got as far as creating an
                             analysis, and only then hit the allowance
analysis IDs returned        accepted, and an analysis really was created
===========================  ==========================================

So while the account's allowance is exhausted, every accepted type reports
itself for nothing. This script exploits that automatically: if a candidate is
accepted but blocked by the allowance it keeps going and enumerates the rest;
if a candidate genuinely creates an analysis it stops immediately, so an
unexhausted account is never drained.

    python3 probe_geneidtype.py --file data.csv --column Common_name \\
        --fc-column Fold_change --skip-rows 1
"""

from __future__ import annotations

import argparse
import sys
from typing import List

import pandas as pd

from ipaapi import ColumnMapping, Dataset, Measurement, MeasurementType, Observation
from ipaapi.auth import TokenCache
from ipaapi.client import IPAClient
from ipaapi.errors import MalformedRequestError, QuotaExceededError, SubmissionError

#: Confirmed already; included so a run re-verifies them.
CONFIRMED = ("ensembl", "hugo")

#: Plausible names for the identifier systems IPA is known to understand.
CANDIDATES = (
    "hgnc",
    "entrezgene",
    "entrez",
    "genesymbol",
    "symbol",
    "genename",
    "refseq",
    "genbank",
    "uniprot",
    "swissprot",
    "unigene",
    "affymetrix",
    "affy",
    "illumina",
    "agilent",
    "mirbase",
    "ipa",
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="a real data file to sample rows from")
    parser.add_argument("--column", required=True, help="identifier column to test")
    parser.add_argument("--fc-column", required=True, help="a fold-change column")
    parser.add_argument("--skip-rows", type=int, default=1)
    parser.add_argument("--rows", type=int, default=2, help="rows per attempt")
    parser.add_argument(
        "--project", default="ipaapi_geneidtype_probe", help="throwaway project"
    )
    parser.add_argument(
        "--candidates",
        nargs="+",
        default=list(CONFIRMED) + list(CANDIDATES),
        help="type strings to try, in order",
    )
    args = parser.parse_args(argv)

    frame = pd.read_csv(args.file, skiprows=args.skip_rows, dtype=object)
    frame = frame[frame[args.column].notna()].head(args.rows)
    if frame.empty:
        print(f"No usable rows in {args.column!r}.", file=sys.stderr)
        return 1

    print(f"Probing {len(args.candidates)} candidate(s) with {len(frame)} row(s).")
    print(f"identifier column {args.column!r}: {list(frame[args.column])}\n")

    client = IPAClient.login(cache=TokenCache())

    accepted: List[str] = []
    rejected: List[str] = []
    created: List[str] = []

    for candidate in args.candidates:
        mapping = ColumnMapping(
            gene_id_column=args.column,
            gene_id_type=candidate,
            observations=[
                Observation(
                    "probe", [Measurement(args.fc_column, MeasurementType.LOG_RATIO)]
                )
            ],
        )
        dataset = Dataset.from_frame(
            frame, mapping, name=f"probe_{candidate}", check_ranges=False
        )

        try:
            ids = client.submit(dataset, project=args.project, reference_set=None)
        except QuotaExceededError:
            # Got far enough to try creating an analysis, so the type is valid.
            # Nothing was created, so this costs nothing -- keep going.
            accepted.append(candidate)
            print(f"  {candidate:<14} ACCEPTED  (blocked by the allowance, so free)")
            continue
        except MalformedRequestError as exc:
            rejected.append(candidate)
            reason = str(exc).strip().splitlines()[0]
            print(f"  {candidate:<14} rejected  ({reason})")
            continue
        except SubmissionError as exc:
            print(f"  {candidate:<14} error     ({str(exc)[:100]})")
            continue

        # A real analysis now exists. Stop rather than spending the allowance.
        created.append(candidate)
        accepted.append(candidate)
        print(f"  {candidate:<14} ACCEPTED  -> created analysis {', '.join(ids)}")
        print(
            "\nStopping: the allowance is not exhausted, so each further accepted "
            "type would create another analysis.\nRe-run while quota-blocked to "
            "enumerate the rest for free."
        )
        break

    print("\n--- summary ---")
    print(f"accepted: {', '.join(accepted) or 'none'}")
    print(f"rejected: {', '.join(rejected) or 'none'}")
    if created:
        print(
            f"\nAnalyses were created in project {args.project!r} for: "
            f"{', '.join(created)}. Delete them in IPA."
        )
    if accepted and not created:
        print("\nNothing was created -- every acceptance was blocked by the allowance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
