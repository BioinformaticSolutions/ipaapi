# Changelog

Versions follow [semantic versioning](https://semver.org). While the package is
pre-1.0, the minor number is bumped for behaviour changes as well as features.

Check what you're running with `ipaapi --version`, which reports the version,
the install location, and whether it's an editable checkout rather than a wheel.

## 0.3.3 — 2026-08-05

### Added

- **`examples/probe_geneidtype.py` can now enumerate the accepted gene ID
  vocabulary for free.** IPA has no endpoint that lists it, but the failure
  modes are distinguishable: an unknown type is rejected before any analysis is
  created, while a *valid* type gets as far as creating one and only then hits
  the allowance. So when the account is quota-blocked, every accepted type
  reports itself at no cost. The script detects this automatically — it
  continues past acceptances that were blocked by the allowance, and stops
  immediately if a candidate genuinely creates an analysis, so an account with
  allowance remaining is never drained.

## 0.3.2 — 2026-08-05

### Changed

- **The allowance rejection is confirmed**: `Unable to run analysis: Analysis
  limit exceeded`, delivered as an HTML page. Added verbatim to
  `QUOTA_PATTERNS` and documented; the remaining patterns stay as guesses at
  other phrasings.
- Page chrome ("About QIAGEN Bioinformatics | Contact Us (c)2000-2026 ...") is
  stripped along with the support boilerplate, so the message reads as just
  the reason.

## 0.3.1 — 2026-08-05

### Fixed

- **Error pages were truncated exactly where the reason lives.** IPA opens its
  error pages with support boilerplate and puts the actual message last; the
  page text was clipped at 300 characters from the front, so a real failure
  read `Unable to run analysis: Analysis ` and stopped. The boilerplate is now
  stripped and the tail preserved.
- **"No files were moved" was printed even when files had been moved.** A
  mid-batch failure files everything submitted before it; the message now says
  what actually happened.
- **"Unable to run analysis" was misreported as a parameter error.** That
  wording means the request reached IPA's analysis logic, so it is not a
  malformed request. New `AnalysisRefusedError` says so, and the run stops and
  leaves the remaining files in place — the right behaviour if the cause is an
  allowance or capacity limit.
- **A quota message delivered as an HTML page** was classified as a bad
  parameter. Quota detection now runs against the whole body before the HTML
  branch.

## 0.3.0 — 2026-08-05

Everything needed for a correct analysis is now established and defaulted.
Confirmed working end to end against live IPA: `hugo` identifiers, `logratio`
fold changes, no `referenceset` — producing populated p-values and FDR.

### Changed — breaking

- **`referenceset` is no longer sent by default.** QIAGEN's demo sent
  `referenceset=dataset`, and this package inherited it unexamined. That makes
  the background the uploaded genes, which for a pre-filtered hit list is the
  same set as the foreground — leaving the enrichment statistics degenerate:
  z-scores produced, p-values and FDR absent. Omitting the parameter lets IPA
  apply its own default, which is confirmed to produce real statistics.

  `IPAClient.submit(reference_set=...)` now defaults to `None`, and
  `--reference-set` defaults to `omit`. Pass `dataset` explicitly to restore
  the old behaviour — appropriate only when uploading a complete measured
  transcriptome.

## 0.2.7 — 2026-08-05

### Added

- **`hugo` confirmed as a gene ID type** for human gene symbols, verified
  against the live API. `CONFIRMED_ID_TYPES` is now `("ensembl", "hugo")`.
  Of the three names in the desktop client's label "Gene Symbol - human
  (HUGO / HGNC / Entrez Gene)", only the first is accepted — `genesymbol` and
  `Gene Symbol` are both rejected.
- README documents the confirmed vocabulary, since QIAGEN does not.

## 0.2.6 — 2026-08-05

### Changed

- **Rejection messages lead with what was rejected.** The explanation used to
  come first and the actual cause several lines down, which is easy to skim
  past and read as success. The first line is now
  `REJECTED: IPA does not recognise the gene ID type 'X'.`, followed by
  `NOTHING WAS SUBMITTED.`, with the reasoning below that.
- Recorded that `Gene Symbol` is rejected as well as `genesymbol` — so the API
  does not take the desktop client's display label either.

## 0.2.5 — 2026-08-05

### Changed

- Gene ID type candidates reordered from evidence rather than convention. The
  IPA desktop client labels a gene symbol column "Gene Symbol - human (HUGO /
  HGNC / Entrez Gene)", so `symbol`, `hugo`, `hgnc` and `entrezgene` are tried
  first. Still guesses — the client and the REST API need not share vocabulary
  — but guesses taken from IPA's own wording.

## 0.2.4 — 2026-08-05

### Fixed

- **Stopped advertising unverified gene ID types.** The help text listed nine
  "common" identifier types that were guesses. `genesymbol` is among the ones
  IPA rejects — "Unknown GeneId Type (genesymbol)" — so the list sent users
  straight into a failed submission. Only `ensembl` is confirmed, and
  `CONFIRMED_ID_TYPES` now holds only values observed to work.

### Added

- **An unrecognised gene ID type is named and explained.** IPA identifies the
  value it rejected in its error page; the message now quotes it, points at the
  `--ID` flag specifically rather than offering a list of suspects, and notes
  that probing costs allowance whenever a candidate is *accepted*.
- `examples/probe_geneidtype.py` tries candidate type strings against a
  two-row extract and stops at the first IPA accepts.

## 0.2.3 — 2026-08-05

### Fixed

- **A mapping that fails every file no longer quarantines the directory.** If
  no file validates, the fault is the command rather than the data, so nothing
  is moved to `failed/` and the run says so. Files only get filed when *some*
  succeed and others don't.

### Added

- **Fold-change rejections suggest `logratio` when the data is log-scaled.**
  A column named "Fold_change" may hold either linear ratios or log2 values.
  When values declared `foldchange` cluster inside (-1, 1) — where fold change
  cannot go but a log ratio spends most of its time — the error now names
  `logratio` as the likely fix instead of only reporting the rejection.

## 0.2.2 — 2026-08-05

### Added

- **Warns when a column declared `logratio` looks like signed fold change.**
  A real log ratio is centred on zero, so its distribution always contains
  values between -1 and 1. Signed fold change (`ratio` if >= 1, else
  `-1/ratio`) can contain none, by construction. A `logratio` column with
  nothing in that interval is therefore almost certainly fold change
  mislabelled — which IPA reads as `2^value`, inflating every magnitude while
  leaving directions intact. Warning only, and skipped below 50 values.

## 0.2.1 — 2026-08-05

### Fixed

- **A rejected parameter no longer quarantines good files.** IPA answers a
  malformed request with an HTML error page; that was being treated as a
  per-file failure, so a bad `--reference-set` moved a perfectly good file into
  `failed/`. An HTML response is now `MalformedRequestError`: the run stops, no
  file is moved, and the message names the likely parameters and quotes what
  the page said.
- Identifier warnings were printed twice per dataset.

### Changed

- **Removed `--reference-set ingenuity`.** It was a guess and IPA rejects it.
- **Added `--reference-set omit`**, which leaves the parameter out of the
  request entirely so IPA applies its own default. For pre-filtered hit lists
  `dataset` makes the background equal the analysis-ready set, which degenerates
  the enrichment statistics — z-scores are still produced but overlap p-values
  are not meaningful.
- Interpret-link failures report the URL, the response body and the JSON keys
  rather than just the status code.

## 0.2.0 — 2026-08-05

First version used against live IPA. Everything below came out of that run.

### Added

- **Command line.** `ipaapi` console script with `validate`, `submit`,
  `status`, `report` and `history` subcommands.
  - `--ID COLUMN:TYPE` and `--FC COLUMN:TYPE[:CUTOFF]`, column positions 0-based.
  - `--ID` may be given twice: the second identifier fills rows where the
    primary is blank.
- **Batch submission.** `PATH` may be a directory; `--pattern` selects files by
  substring or glob, `--recursive` descends. One dataset and analysis per file.
- **File triage.** Directory submits move each file as its outcome is known:
  `submitted/` when IPA accepts it, `failed/` (with a `.error.txt` note) when
  the file is at fault, left in place when the allowance is exhausted. Those
  folders are excluded from discovery, so a re-run resumes exactly where it
  stopped.
- **Quota detection.** `QuotaExceededError`, raised on HTTP 429 or when the
  response matches `client.QUOTA_PATTERNS`, so an exhausted allowance is not
  mistaken for a broken file. Heuristic — IPA's wording is undocumented, and
  the raw response body is always reported.
- **Submission log.** Every analysis appends a timestamped row to
  `~/.local/state/ipaapi/submissions.tsv`; `ipaapi history` reads it back with
  `--project`, `--since`, `--limit` and `--status`. IPA's API cannot enumerate
  analyses, so without this a lost terminal means a lost ID.
- **Token refresh.** An expired cached token is renewed from its refresh token
  over HTTP, with no browser and no prompt. Makes headless and copied-token
  workflows self-sustaining. `--token-file` points at a cache copied from
  another machine.
- `IPAAPI_TOKEN_FILE` and `IPAAPI_LOG_FILE` environment variables, for hosts
  where the home directory isn't writable. An unwritable token cache means a
  fresh login on every run, so both failures now say how to fix them.
- `--skip-rows N` for files with a comment or title line above the header.
- `--browser NAME`, and a login failure message that distinguishes `DISPLAY`
  unset from no browser found, pointing at `ssh -X` or a port forward.

### Changed

- **`submit` no longer waits by default.** It returns once the analyses are
  queued and prints the `status`/`report` commands; `--wait` restores polling.
  `--no-wait` is still accepted so existing commands keep working.
- Observation and dataset names come from the filename rather than the
  fold-change column header, so a batch doesn't produce analyses all named
  "log2FC".
- Directory submits validate per file; a malformed file goes to `failed/`
  instead of aborting the batch. `validate` remains all-or-nothing.

### Fixed

- **Delimiter sniffing read line 1 rather than the header**, so a comment line
  containing commas caused the whole file to be parsed as CSV — silently, with
  the comment becoming the column names. A header that looks like a comment is
  now rejected with an explanation.
- **`report` on an unfinished analysis** reported a bare HTTP 500. It now
  checks status first and says the analysis is still running.
- **Token cache write failures were swallowed**, which looked identical to a
  token expiring instantly.

## 0.1.0 — 2026-08-05

Initial package, built on QIAGEN's `python-api-demo` example code.

### Added

- `ColumnMapping` / `Observation` / `Measurement`: columns addressed by name in
  any order, validated against the data before upload, replacing the demo's
  fixed positional layout. `ColumnMapping.from_blocks()` reproduces the old
  behaviour for migration.
- `IPAClient` with `submit`, `status`, `wait_for`, `results` and `report_url`.
- OAuth 2.0 + PKCE login with a disk token cache.
- Typed exception hierarchy under `IPAError`.

### Fixed relative to the demo

- **Request bodies are percent-encoded.** The demo concatenated them by hand,
  so any value containing a space, `&`, `=`, `+` or `%` corrupted the request —
  including the `Group Max Intensity` column in the demo's own sample dataset.
- OAuth no longer spins a CPU core waiting for the callback, validates the
  `state` parameter, times out, shuts its server down, and handles an error
  redirect instead of waiting forever.
- Submissions are never retried automatically, since a retried POST could
  create duplicate analyses. GETs retry with backoff.
- Access tokens are excluded from `repr()`.
