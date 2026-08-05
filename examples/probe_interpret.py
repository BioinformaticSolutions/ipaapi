"""Show exactly what IPA returns for an interpret-link request.

The Interpret link endpoint is inherited from QIAGEN's demo code and is not
publicly documented, so when it fails there is nothing to check the call
against. This prints the raw status, headers and body so the failure can be
read rather than guessed at.

Run on a machine with a working ipaapi token::

    python3 probe_interpret.py 43595039

By default only the endpoint the package actually uses is contacted. ``--guess``
additionally tries two plausible alternative paths -- these are speculation, so
they are opt-in rather than fired at QIAGEN's servers unasked.
"""

from __future__ import annotations

import argparse

import requests

from ipaapi.auth import TokenCache
from ipaapi.client import IPAClient


def probe(url: str, headers: dict, timeout: float = 60.0) -> None:
    """Contact *url* and print everything useful about the response."""
    print(url)
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        print(f"  request failed: {exc}\n")
        return

    body = (response.text or "").strip()
    print(f"  HTTP {response.status_code}")
    print(f"  content-type: {response.headers.get('content-type')!r}")
    print(f"  body[:800]: {body[:800]!r}")
    try:
        payload = response.json()
    except ValueError:
        pass
    else:
        if isinstance(payload, dict):
            print(f"  JSON keys: {sorted(payload)!r}")
    print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis_id")
    parser.add_argument(
        "--application-name",
        default="PythonAPI",
        help="must match the applicationname the analysis was submitted under",
    )
    parser.add_argument(
        "--guess",
        action="store_true",
        help="also try undocumented alternative paths (speculative)",
    )
    args = parser.parse_args(argv)

    client = IPAClient.login(
        cache=TokenCache(), application_name=args.application_name
    )
    host = client.host
    app = client.application_name
    headers = client.credentials.auth_header

    print(f"analysis {args.analysis_id}")
    print(f"host {host}, applicationname {app!r}")
    try:
        print(f"status: {client.status(args.analysis_id).name.lower()}")
    except Exception as exc:
        print(f"status: could not be read ({exc})")
    print()

    # The endpoint the package uses, taken from QIAGEN's demo code.
    probe(
        f"https://{host}/pa/ipa/analysisResults/interpretLink/{app}/{args.analysis_id}",
        headers,
    )

    if args.guess:
        print("--- speculative paths below; these may simply not exist ---\n")
        for url in (
            f"https://{host}/pa/api/v2/interpretlink"
            f"?applicationname={app}&analysisuid={args.analysis_id}",
            f"https://{host}/pa/ipa/analysisResults/interpretLink/{args.analysis_id}",
        ):
            probe(url, headers)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
