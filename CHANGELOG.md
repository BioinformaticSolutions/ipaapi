# Changelog

Versions follow [semantic versioning](https://semver.org). While the package is
pre-1.0, the minor number is bumped for behaviour changes as well as features.

Check what you're running with `ipaapi --version`, which reports the version,
the install location, and whether it's an editable checkout rather than a wheel.

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
