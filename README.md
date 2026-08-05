# ipaapi

A Python package for QIAGEN Ingenuity Pathway Analysis (IPA): upload a dataset
into an IPA project using an **explicit column mapping**, submit it for
analysis, and track or retrieve the results.

Built on QIAGEN's `python-api-demo` example code. Not an official QIAGEN product.

## Why this exists

The demo code works, but assumes a rigid file layout: gene ID in column 0, then
`n_observations x n_measurements` value columns in strict repeating order, every
observation carrying the same measurement types in the same positions. Real
files rarely look like that.

This package replaces that assumption with a declaration. You name the gene ID
column and describe each observation as a set of `(column, measurement type)`
pairs. Columns may be in any order, named anything, and interleaved with columns
the analysis should ignore.

## Install

```bash
pip install -e .
```

Requires Python 3.9+, `requests`, `requests-oauthlib`, `pandas`.

## Command line

Installing puts `ipaapi` on your PATH:

```bash
ipaapi --help
ipaapi submit --help
```

Column positions are **0-based** — `--ID 0` is the first column in the file.

```bash
# check a mapping without contacting IPA
ipaapi validate rnaseq.txt --ID 0:ensembl --FC 1:foldchange

# upload into a project and start the analysis
ipaapi submit rnaseq.txt --ID 0:ensembl --FC 1:foldchange:1.5 --project Study1

# ... and block until it finishes, printing the report link
ipaapi submit rnaseq.txt --ID 0:ensembl --FC 1:foldchange:1.5 --project Study1 --wait

# check on / fetch links for existing analyses
ipaapi status abc-123 abc-124
ipaapi report abc-123 --open
```

`--ID` and `--FC` are required.

| Flag | Form | Meaning |
| --- | --- | --- |
| `--ID` | `COLUMN:TYPE` | 0-based identifier column and its IPA gene ID type |
| `--FC` | `COLUMN:TYPE[:CUTOFF]` | 0-based fold-change column, measurement type, optional cutoff |
| `--pattern` | `TEXT` | when PATH is a directory, which files to use (substring or glob) |
| `--recursive` | flag | search subdirectories too |
| `--skip-rows` | `N` | discard N lines above the header row |
| `--sep` | `CHAR` | field delimiter (sniffed from the header line by default) |

### Waiting, or not

`submit` returns as soon as the analyses are queued and tells you how to check
on them:

```
submitted SampleA_DEG: abc-123

Submitted 1 analysis.
Analyses are running in IPA. Check on them with:
  ipaapi status abc-123
  ipaapi report abc-123
Or re-run with --wait to block until they finish.
```

Add `--wait` to poll instead, printing each analysis's Interpret link as it
completes. `--interval` and `--timeout` tune the polling (default: every 30s,
give up after an hour); they only apply with `--wait`.

Analyses run on QIAGEN's servers, so nothing is lost by not waiting — and
interrupting a `--wait` run with Ctrl-C doesn't cancel anything either.

### Files are filed as they are processed

When `PATH` is a directory, each file is moved as its outcome becomes known:

| Outcome | Where the file goes |
| --- | --- |
| IPA accepted it | `submitted/` — done, never resubmitted |
| The file is the problem | `failed/`, with a `.error.txt` note beside it |
| Allowance exhausted | left in place for the next run |

```
failed validation SampleBAD_DEG.txt: --FC refers to column 1, but the file has only 1 column(s)
submitted SampleA_DEG: 43595001
submitted SampleB_DEG: 43595002

Allowance exhausted while submitting SampleC_DEG:
IPA said: 'Monthly analysis quota exceeded'

2 file(s) moved to submitted/
1 file(s) moved to failed/
2 file(s) left in place for the next run
Re-run the same command once the allowance resets; the files left in place are
exactly the ones still to do.
```

So the source directory shrinks to exactly the work outstanding, and re-running
the identical command picks up where it stopped. `submitted/` and `failed/` are
excluded from discovery, so a second run can't re-ingest its own output. The
folders are created only when something needs filing, and `--dry-run` reports
the moves without making them.

Single-file submits are never moved — filing only applies to a directory.

> **Quota detection is a heuristic.** IPA's response for an exhausted allowance
> isn't documented, so it's matched on HTTP 429 plus wording like "quota",
> "allowance", "exceeded" (see `ipaapi.client.QUOTA_PATTERNS`). The raw response
> body is always printed, so a misclassification is visible rather than silent.
> If you hit a real quota rejection and the wording differs, the printed body
> will say so and the pattern list is a one-line fix.

### Finding analysis IDs later

IPA's API cannot list the analyses on an account — every endpoint needs an ID
you already hold. So the package keeps its own log: every submitted analysis
appends a timestamped row to `~/.local/state/ipaapi/submissions.tsv`.

```bash
ipaapi history
ipaapi history --project singlet_RNA_P05
ipaapi history --since 2026-08-01 --limit 20
ipaapi history --status              # look up each analysis's current state
```

```
2026-08-05T08:35:53-06:00  43595039  singlet_RNA_P05  SampleA_DEG
2026-08-05T08:35:53-06:00  43595041  singlet_RNA_P05  SampleB_DEG
...
7 submission(s). Report links: ipaapi report 43595039 43595041 ...
```

It's a plain TSV — grep it, open it in Excel, whatever. `--log-file` points at a
different one. The log covers submissions made through this tool only; analyses
submitted from the IPA client won't appear.

### Comment lines above the header

Files often carry a provenance or title line before the real header:

```
# RNAseq DE results, pipeline v3, run 2026-08-05
EnsemblID	log2FC	pval
ENSG001	2.4	0.01
```

`--skip-rows N` discards those lines:

```bash
ipaapi validate SampleA_DEG.txt --skip-rows 1 --ID 0:ensembl --FC 1:logratio
```

Column numbers always count from the **header row**, so they don't change when
you add `--skip-rows` — in the file above `EnsemblID` is column 0 either way.

Skipping also fixes delimiter detection. The delimiter is sniffed from the
header line, and a comment line is a bad thing to sniff: the one above contains
commas but no tabs, so without `--skip-rows` the file would be read as CSV and
collapse into two nonsense columns. Rather than let that through, a header that
looks like a comment (or a file that parses to a single column) is rejected with
a message pointing at this flag.

### Many files at once

`PATH` may be a directory instead of a file. `--pattern` picks which files in it
to use, and each matched file becomes its own dataset and its own analysis,
named after the file:

```bash
# every .txt / .tsv / .csv in the folder
ipaapi submit ~/data --ID 0:ensembl --FC 1:foldchange --project Study1

# only files whose name contains "SampleA"
ipaapi submit ~/data --pattern SampleA --ID 0:ensembl --FC 1:foldchange --project Study1

# glob syntax, searching subfolders too
ipaapi submit ~/data --pattern "*_DEG.tsv" --recursive \
    --ID 0:ensembl --FC 1:foldchange --project GroupB
```

`--pattern` takes plain search text or a glob. Text with no `*`, `?` or `[`
matches as a **substring**, so `--pattern SampleA` finds `SampleA_DEG.txt` and
`SampleA_raw.tsv`. Text containing glob characters is used verbatim. With no
`--pattern`, the common delimited-text extensions are searched. Hidden files are
skipped; results are sorted so run order is predictable.

Every matched file must fit the same `--ID`/`--FC` column positions. **All files
are validated before any is uploaded**, so a bad file at position 7 of 10 fails
the run without leaving six analyses stranded in your project:

```
error: 1 of 3 file(s) do not fit the mapping, so nothing was uploaded:
  - SampleZ_DEG.txt: --FC refers to column 1, but the file has only 1 column(s) ...
```

Run `validate` first to see exactly what matched:

```bash
ipaapi validate ~/data --pattern SampleA --ID 0:ensembl --FC 1:foldchange
```

If a *submission* fails partway through a batch (a network drop, say), the run
continues with the remaining files and reports which ones failed, rather than
abandoning the ones already uploaded.

Because names come from filenames in batch mode, `--observation`,
`--analysis-name` and `--dataset-name` only apply when a single file is
selected. Use `--project` to group a batch.

### Two identifier columns

`--ID` may be given twice. The first is the **primary**; the second is used only
for rows where the primary is blank:

```bash
ipaapi submit rnaseq.txt --ID 0:ensembl --ID 4:genesymbol \
    --FC 1:foldchange --project Study1
```

> **Read this before relying on it.** IPA accepts a *single* `geneidtype` per
> submission. Rows filled from a second identifier of a different type are still
> uploaded under the primary's type declaration, so IPA may fail to map them.
> The package always reports how many rows were filled:
>
> ```
> Warning: 1 of 4 rows took their identifier from the fallback column 'Symbol'
> (genesymbol). IPA is told a single gene ID type for the submission --
> 'ensembl' -- so those rows are uploaded under that declaration and may not map.
> ```
>
> If that number is large, consider `--ID` with the type that covers most rows,
> or submit twice, once per identifier type.

Rows where *both* identifiers are blank are reported separately; if every row
lacks an identifier the run aborts, which usually means the column number is
wrong or the file has no header row.

Only identifier and fold-change columns are exposed on the command line. For
p-values, FDR, intensity, or multiple observations, use the Python API below.

## Quick start

```python
from ipaapi import (
    ColumnMapping, Dataset, IPAClient, Measurement, MeasurementType, Observation,
)

mapping = ColumnMapping(
    gene_id_column="Gene IDs",
    gene_id_type="ensembl",
    observations=[
        Observation("Gemfib vs ctrl", [
            Measurement("FoldChange", MeasurementType.FOLD_CHANGE, cutoff=1.5),
            Measurement("PValue", MeasurementType.P_VALUE),
            Measurement("AdjustedPValue", MeasurementType.FALSE_DISCOVERY, cutoff=0.01),
            Measurement("Group Max Intensity", MeasurementType.INTENSITY),
        ]),
    ],
)

dataset = Dataset.from_file("Data/Gemfibrozil vs Ctrl RNAseq.txt", mapping)
print(dataset.describe())        # confirm the mapping before uploading

client = IPAClient.login()       # opens a browser for OAuth
analysis_ids = client.submit(dataset, project="PythonAPI_Demo")
statuses = client.wait_for(analysis_ids)

for analysis_id, status in statuses.items():
    if status.succeeded:
        print(client.report_url(analysis_id))
```

## Column mapping

`ColumnMapping` validates before anything is uploaded, so a mistake costs a
traceback rather than a failed 3 MB request:

- every declared column exists in the file;
- no column is claimed by two observations;
- values fall inside the range IPA accepts for their declared measurement type
  (p-values in `[0,1]`, fold changes outside `(-1,1)`, and so on) — opt out with
  `check_ranges=False`;
- the measurement types and cutoffs are consistent across observations.

That last rule is imposed by the IPA API, not by this package. The wire format
declares `expvaltype`, `expvaltype2`, ... and `cutoff`, `cutoff2`, ... **once for
the whole submission**, then supplies per-observation column names against those
slots. So every observation must contribute exactly one column per measurement
type, and a given type carries one cutoff throughout. Within those limits, order
and naming are entirely free — observations declared in different column orders
are normalised into canonical slot order automatically.

### Multiple observations

```python
mapping = ColumnMapping(
    gene_id_column="Gene IDs",
    gene_id_type="ensembl",
    observations=[
        Observation("drug A vs ctrl", [
            Measurement("A_log2fc", MeasurementType.LOG_RATIO),
            Measurement("A_padj",   MeasurementType.FALSE_DISCOVERY, cutoff=0.05),
        ]),
        Observation("drug B vs ctrl", [
            # declared in a different order on purpose -- this is fine
            Measurement("B_padj",   MeasurementType.FALSE_DISCOVERY, cutoff=0.05),
            Measurement("B_log2fc", MeasurementType.LOG_RATIO),
        ]),
    ],
)
```

One analysis is launched per observation; `submit()` returns one ID per
observation, in mapping order.

### Migrating from the demo

`ColumnMapping.from_blocks()` reproduces the old positional behaviour, so an
existing `ipa_analyze(...)` call can be ported without re-describing the file:

```python
mapping = ColumnMapping.from_blocks(
    columns=list(frame.columns),
    gene_id_type="ensembl",
    observation_names=["Gemfib vs ctrl"],
    measurement_types=["foldchange", "pvalue", "falsediscovery", "intensity"],
    cutoffs=[1.5, None, 0.01, None],
)
```

## Authentication

`IPAClient.login()` runs the browser OAuth 2.0 + PKCE flow. The default client
ID is the public one any IPA user may use; it is not a secret.

Changes from the demo's flow:

- the callback is awaited on an `Event` rather than a spin loop that pegged a CPU core;
- the OAuth `state` parameter is verified instead of discarded (CSRF);
- login times out rather than hanging forever;
- the callback server is always shut down, so a second login in one process works;
- an `error` redirect raises instead of waiting forever;
- tokens can be cached to disk, so repeat runs skip the browser:

```python
from ipaapi import IPAClient, TokenCache
client = IPAClient.login(cache=TokenCache())
```

The redirect URI must match the OAuth client registration — for the default
public client that is `http://localhost:8000`, so the callback port is 8000.

### Headless servers

Access tokens from QIAGEN are short-lived, but a **refresh token** comes with
them, and the package spends it automatically: when the cached token has
expired, it renews silently over HTTP with no browser and no prompt. A browser
login is only needed when the refresh token itself is rejected.

That makes the copy-a-token workflow practical on a machine with no browser:

```bash
# on a Mac, once
ipaapi validate anything.txt --ID 0:ensembl --FC 1:foldchange   # or any submit

# copy the cache across
scp ~/.cache/ipaapi/token.json server:~/.cache/ipaapi/token.json
ssh server chmod 600 ~/.cache/ipaapi/token.json
```

From then on the server renews its own token. `--token-file PATH` points at a
cache somewhere other than `~/.cache/ipaapi/token.json`.

When a login *is* genuinely needed — the refresh token was rejected — the
cleanest answer is X forwarding:

```bash
ssh -X you@server        # ssh -Y from macOS, with XQuartz running
```

A browser installed on the server then renders on your local display, and the
redirect to `localhost:8000` resolves on the server where the callback is
listening — so no port forwarding is needed. `--browser firefox` names a
specific one if the default pick is wrong.

If there's no browser on that machine, forward the callback port instead and use
your own:

```bash
ssh -L 8000:localhost:8000 you@server
```

Run `ipaapi` inside that session; it prints the authorization URL, you open it
in your laptop's browser, and the redirect comes back down the tunnel. When no
browser can be opened, the error says which of these applies — whether `DISPLAY`
is unset, or set but with no browser found.

Already have a token from elsewhere:

```python
from ipaapi import Credentials, IPAClient
client = IPAClient(Credentials.from_token(os.environ["IPA_TOKEN"]))
```

## Results

```python
results = client.results(analysis_id)
print(results.canonical_pathways.head())
print(results.upstream_regulators.head())
print(results.bio_functions.head())

cp, ur, df = results          # unpacks like the demo's ipa_results()
```

> Programmatic result retrieval is a **commercial IPA add-on**. Without that
> licence these calls raise `ResultsUnavailableError`. Submission, status
> polling and report URLs are unaffected — the finished analysis can always be
> opened in IPA itself.

## Other differences from the demo

- **Request bodies are percent-encoded.** The demo built the body by string
  concatenation, so any gene ID, column header or observation name containing a
  space, `&`, `=`, `+` or `%` silently corrupted the request. The sample dataset
  shipped with the demo contains a column called `Group Max Intensity`.
- **Submissions are never retried automatically.** GETs retry with backoff on
  429/5xx; a retried POST could create duplicate analyses.
- **Errors are typed.** Everything derives from `IPAError`; see `ipaapi.errors`.
- **Tokens never appear in `repr()`.**
- **No `install_dependencies()` shelling out to `pip3`.** Dependencies are
  declared in `pyproject.toml`.

## Layout

```
src/ipaapi/
  __init__.py    public API
  models.py      MeasurementType, AnalysisStatus, ReferenceSet
  mapping.py     Measurement, Observation, ColumnMapping
  dataset.py     Dataset, load_table
  _payload.py    multiobsanalysis body construction
  auth.py        OAuth 2.0 + PKCE login, Credentials, TokenCache
  client.py      IPAClient, AnalysisResults
  cli.py         ipaapi console script
  errors.py      exception hierarchy
tests/           offline unit tests (no network required)
examples/        runnable end-to-end script
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite is fully offline: mapping validation, the exact parameter layout of
the submission body, encoding of hostile characters, and status/ID parsing.

## Status

Early. The submission path is exercised end-to-end against the demo's 32,883-row
sample dataset; the API surface may still change.
