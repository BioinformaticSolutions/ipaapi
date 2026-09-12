# Changelog

Versions follow [semantic versioning](https://semver.org): breaking changes to
the command line or the Python API bump the major number, additions bump the
minor, fixes bump the patch.

Check what you're running with `ipaapi --version`, which reports the version,
the install location, and whether it's an editable checkout rather than a wheel.

## Unreleased

Found by reading the package end to end three times rather than by hitting them
in use. Every item below was reproduced before being written down. Held
together for one release instead of being spent a version number at a time.

### Wrong data reaches IPA, silently

These are the failure the package exists to prevent: IPA accepts the
submission, reports success, and scores an analysis on data that is not what
was meant.

- **Non-finite values clear the range check and are sent as text.**
  `is_plausible` returns True for `inf` under `foldchange` (`inf >= 1`),
  `logratio` and `other` (unbounded), and `ratio` and `intensity` (upper bound
  is `inf`). `_format` then hands IPA the literal string `Inf`. DESeq2 and
  edgeR emit `Inf`/`-Inf` whenever a group has zero counts, so this is ordinary
  output. Reject non-finite values in `_check_ranges`, naming the rows.

- **The range check silently ignores every cell it cannot parse.**
  `_check_ranges` does `pd.to_numeric(..., errors="coerce").dropna()`, so
  non-numeric cells are discarded *before* the plausibility test and an
  entirely non-numeric column produces an empty series and is skipped. A `--FC`
  off by one column validates clean and uploads gene symbols as fold changes:
  `sent: ['TP53', 'BRCA1']`. Excel artefacts and European decimals do the same:
  `sent: ['2.0', '#DIV/0!', '1,5']`. Count what could not be parsed and refuse
  a column that is mostly or entirely unparseable.

- **The delimiter is sniffed from the header line alone, so one comma in a
  column name shreds a TSV.** A tab-separated file whose header reads
  `Gene ID<TAB>log2FC, shrunken<TAB>p-value` parses as two columns named
  `'Gene ID\tlog2FC'` and `'shrunken\tp-value'`. `_warn_if_header_looks_wrong`
  does not fire (two columns, no comment prefix), the CLI resolves `--ID 0` and
  `--FC 1` positionally onto the mangled names, the all-NaN value column hits
  the skip path above, and IPA receives unmappable identifiers and no
  measurements. Sniff over several lines and require a consistent field count.

- **Duplicate column labels in the frame shift every value into the wrong
  measurement slot.** `frame.loc[:, value_columns]` returns one column per
  match, so a repeated header lengthens the row while `offset % n_slots` keeps
  cycling. With two observations of (fold change, p-value) and `FC_A` appearing
  twice, observation B receives a p-value in its fold-change slot and a fold
  change in its p-value slot. `validate()` checks the mapping for repeats but
  never the frame. Reject duplicate labels among the mapped columns.

- **`-` and `.` are sent as expression values.** `mapping._BLANK_TOKENS`
  declares them missing-value spellings and honours them for identifiers;
  `_payload._MISSING` does not list them, and pandas does not treat them as NA.
  Common in microarray and proteomics exports. Share one vocabulary.

- **A float-typed identifier column uploads as `7157.0`.** One blank cell
  promotes an int64 Entrez column to float and every identifier gains `.0`, so
  nothing maps. The CLI forces `dtype=object` and is safe; `Dataset.from_frame`
  with a caller's own frame is a documented entry point and is not.

- **`gain_loss` and `classification` are range-checked as continuous.**
  `_RANGES` gives both `(-2, 2)` while the README documents them as the
  discrete set -2, -1, 0, 1, 2, so a continuous copy-number log ratio passes
  validation and is then discarded by IPA without comment.

### Errors are misclassified, and the batch acts on the wrong diagnosis

- **The quota classifier fires on the bare word "exceeded" or "limit
  reached".** It is checked first in `_raise_submission_error`, so it wins over
  every other branch: "Maximum upload size exceeded" and "Observation name
  length exceeded the maximum permitted" both raise `QuotaExceededError`.
  `cmd_submit` reads quota as "the file is fine, IPA is busy", breaks out of
  the loop, leaves that file and every file after it in place, and reports an
  exhausted allowance. The file is never quarantined, so the next run repeats
  it -- a permanent per-file error becomes an unbounded retry loop wearing the
  wrong diagnosis. Require the confirmed wording.

- **A bare 502 or 504 anywhere in the body reads as a gateway timeout.**
  `\b50[24]\b` matches "Unable to run analysis: 504 identifiers could not be
  mapped", and the timeout branch is checked before the refusal branch, so a
  genuine refusal is reported with "your command and your data are almost
  certainly fine". Narrower than it looks -- `504px` and `Sample_504_x` do not
  match -- but a count rendered as a bare number does.

- **`status()` accepts any HTTP-200 body.** `AnalysisStatus.from_code` returns
  `IN_PROGRESS` for anything that is not exactly 3, 4 or 5, and this package
  documents elsewhere that IPA delivers its errors inside HTTP 200. So an error
  page reads as "still running": `wait_for` polls out its whole hour and then
  reports `in_progress`, which reads as a stuck analysis rather than "we never
  got a valid answer".

### The CLI reports success it did not have

- **`submit` exits 0 after quarantining files into `failed/`.** Validation
  failures are printed and filed but never added to `failures`, which is the
  only thing the exit code consults. Reproduced: one broken file of three,
  `failed/` contains it, `EXIT CODE: 0`. A cron or Snakemake wrapper checking
  `$?` sees success. `submit --dry-run` has the same hole and exits 0 where
  `validate` on the same directory exits 1.

- **An interrupted batch loses the IDs of analyses IPA has already accepted.**
  `triage.mark_submitted()` runs per file inside the loop; `history.append()`
  runs once after it. Ctrl-C on file two of a batch leaves file one filed under
  `submitted/` -- so a re-run will not resubmit it -- with no log written and
  its analysis ID only in the scrollback. That is precisely the loss the
  history module was written to prevent, and its docstring claims a line is
  appended at a time. Append per file.

- **`--recursive` descends into hidden directories.** The dot-check looks only
  at the filename, so `.ipynb_checkpoints/x-checkpoint.txt` -- a stale copy of
  a real file -- is submitted as an extra analysis, burning allowance, and then
  filed into `submitted/`.

- **Observation names can collide after the final trim.** Every earlier
  shortening step is guarded by `_usable`, whose job is to keep names distinct.
  `trim_name` is applied unconditionally and never re-checked, and it drops the
  middle. Two files differing only in the middle collide once a third,
  unrelated file in the batch defeats the shared-affix stripping that would
  otherwise have saved them -- an ordinary state for a directory. Both analyses
  then land in IPA with identical `obs1name`, which is what breaks a comparison
  analysis.

- **`status` and `report` abort on the first bad analysis ID.** The call is
  unguarded, so one stale ID means every ID after it goes unchecked --
  including in the `ipaapi status <every id>` line `submit` itself prints.
  `history` guards the identical call.

- **`trim_name` can reduce a name to two characters.** The `name[:limit]`
  fallback fires only when head *and* tail are empty, so a long unbroken token
  followed by a short one yields `..rep` or `..v2`. `MIN_STRIPPED_NAME` is not
  applied here. Common for GEO-derived, camelCase filenames.

- **The cutoff token pattern matches any bare integer, and `p53`.** The decimal
  point is optional in `^(p|q|padj|fdr|adj|log2fc|fc|lfc)?[0-9]*\.?[0-9]+$`, so
  `p53`, `q30`, `fc2`, `10` and `100` all read as statistical cutoffs and are
  dropped as disposable. A dose of 10 versus 100, or a `_p53` subset, is
  removed from the observation name while the batch stays distinct for another
  reason, so `_usable` never objects.

### Hangs, crashes and lost state

- **`login()` hangs forever despite its timeout.** `_CallbackServer` is a
  single-threaded `HTTPServer` with no handler timeout, so one connection that
  never completes a request blocks the loop in `rfile.readline()`. The real
  redirect is then never processed, and `server.shutdown()` in the `finally`
  waits for `serve_forever` to exit and never returns -- so the
  `AuthenticationError` the timeout raised is never delivered and the process
  sits silent. Verified: at 10s, with `timeout=2.0`, the main thread is in
  `socketserver.shutdown()` at auth.py:648. A browser preconnecting to
  localhost:8000, or any security agent probing loopback, is enough.
  `ThreadingHTTPServer` plus a handler timeout fixes both halves. This is the
  headline feature of 1.3.0.

- **Concurrent logins lose tokens, and blame the wrong thing.**
  `TokenCache.put` writes to a fixed `<path>.tmp`, so two processes race: one
  `os.replace` finds the temp file already renamed away, the entry is lost, and
  the handler tells the user their cache location is unwritable and to set
  `IPAAPI_TOKEN_FILE`. With three workers over a 40-entry cache the file went
  from 37,780 to 2,823 bytes. Any parallel batch triggers it, which is the
  workflow this package is for. Use `tempfile.mkstemp` in the same directory.

- **The token cache is world-readable while the token is in it.** `put` opens
  the temp file with `open(tmp, "w")`, writes the access and refresh tokens,
  and only then chmods to 0600. Measured at 0644 mid-write. Matters on exactly
  the machine this tool is built for. Create it with
  `os.open(..., O_CREAT | O_EXCL, 0o600)`.

- **One non-UTF-8 byte in the log blocks `submit` entirely.** `history.read`
  catches only `OSError`, but `UnicodeDecodeError` is a `ValueError`, and
  `_already_submitted` calls `read` for every dataset on the normal submit
  path. A row written by Excel's tab-delimited export on Windows is enough --
  and the module markets the file as spreadsheet-readable. Logging is supposed
  to be best-effort; here it is load-bearing.

- **A truncated final row crashes `ipaapi history`.** `csv.DictReader` fills
  missing keys with `None`, `.get(key, "")` returns that `None` because the key
  exists, and the column-width computation raises
  `TypeError: object of type 'NoneType' has no len()`. An interrupted write or
  a full disk leaves exactly that.

- **`Triage.summary()` reports files as moved when the move failed.** The lists
  are appended to before `_move` is attempted, and `_move` returns `None` on
  failure. On a read-only input directory the run prints two warnings saying
  the files were left in place and then "2 file(s) moved to submitted/". The
  summary is the run's verdict on what still needs doing.

- **A corrupt `expires_at` raises instead of reading as a cache miss.** The
  `try/except` covers `Credentials.from_dict` but `is_expired` is evaluated
  outside it, so a string or list there gives a `TypeError`. The docstring
  promises a corrupt cache is treated as a miss.

### Smaller

- `auth.py` tells a user with a missing dependency to run
  `pip install requests-oauthlib`; a bare `pip` does not exist on macOS or most
  Linux distributions, and this branch is reached precisely when the
  environment is already confused about what is installed where. Use
  `sys.executable`. The sibling raise on the refresh path offers no remedy.
- The FDR percentage warning says to multiply the column by 100 but does not
  say that `--fdr`'s cutoff is on the same scale and is not multiplied, so
  following the advice makes filtering 100x stricter, silently.
- Entity names are run through `value.replace("+", " ")`, so `NAD+ Signaling`
  becomes `NAD  Signaling`.
- Passing your own `requests.Session` silently discards your adapters; the
  docstring advertises it as the way to bring your own configuration.
- `--help` appends `(default: None)` after help text that already states a
  different default, for `--pattern`, `--observation` and `--log-file`.
- `history --limit 0` means no limit, because 0 is falsy; negative values slice
  from the front.
- Timestamps are written with a local UTC offset and compared lexicographically,
  so `--since` returns the wrong rows across a clock change, or permanently when
  two machines in different zones share one log.
- Cutoffs are rendered with `%g`, i.e. six significant digits.
- `_write_note` overwrites an existing `.error.txt` rather than going through
  `_unique` as `_move` does; reachable because `TABLE_PATTERNS` matches
  `*.error.txt` on a later run.
- A `#`-prefixed header is rejected as a comment, and the `--skip-rows 1` it
  advises then promotes the first data row to the header.
- `GatewayTimeoutError` is missing from `errors.__all__`.

## 1.3.0 — 2026-09-12

### Added

- **`--pvalue COLUMN[:CUTOFF]` and `--fdr COLUMN[:CUTOFF]`**, for unfiltered
  tables that still carry their statistics. The measurement type comes from the
  flag, so there is no `:TYPE` to get wrong. Slot order is fixed — fold change,
  then p-value, then FDR — because the wire format declares the slots once for
  the whole submission and then fills them positionally.

  A submission using only `--FC` encodes byte for byte as it did in 1.2.0.

- **A warning when an FDR column looks like a fraction.** IPA reads
  `falsediscovery` as a **percentage** in [0, 100], while statistical software
  emits q-values in [0, 1]. Both are inside the accepted range, so nothing is
  rejected and nothing is discarded — a q-value of 0.05 is simply taken as
  0.05%, and any cutoff applied in IPA silently keeps far less than intended.
  This is the one measurement type where the range check cannot help, because
  the wrong scale is a valid value, so the warning fires on the shape of the
  distribution instead.

- **`ipaapi login`**, to authenticate and cache a token without submitting
  anything. Reports where the token was written, when it expires, and whether a
  refresh token was issued. Previously the only way to find out whether
  credentials worked was to spend analysis allowance finding out. `--force`
  signs in again, `--forget` clears the cache.

  It deliberately offers no `--client-id` or `--host`: the cache is keyed on
  those, so a login under a different key would be invisible to every other
  command — a login that appears to work and changes nothing.

### Fixed

- **A gateway timeout is no longer reported as a bad parameter.** IPA delivers
  these in the body of an HTTP 200, like its other errors, so the existing
  502/503/504 status check never saw them and the response fell through to
  "IPA would not accept one of the submission parameters" — pointing at
  `--reference-set` and `--ID`, neither of which had anything to do with it.

  The new message names the two known causes in order: an over-long observation
  name, which has been observed to time out as well as to be rejected outright,
  and a slow transfer over a congested link or VPN. It also warns that a
  timeout loses the *answer* and not necessarily the *request*, so the file may
  have been created and a retry may collide with it.

  Raised as `GatewayTimeoutError`, a subclass of `ServiceUnavailableError` so
  existing handlers keep working.

- **A held OAuth port now names what is holding it.** The redirect URI is
  registered with the OAuth client and cannot be changed, so a collision has to
  be resolved by dealing with the process — which is very often a previous
  `ipaapi login` that never exited. `ipaapi` now identifies it via `lsof` or
  `ss`, says so, and prints the `kill` command. When neither tool is present it
  prints the diagnostic command for the platform instead.

## 1.2.0 — 2026-08-24

### Fixed

- **Long observation names are shortened automatically.** IPA rejects an
  observation name past roughly 65 characters and reports it as *"The page you
  are looking for is currently unavailable"* — the same page it returns for a
  duplicate dataset name, and for a genuine outage. Because the observation
  name defaults to the filename, descriptive pipeline output names tripped it,
  and a batch died on its first file looking like a total service failure.

  Established by A/B on one file, holding project, reference set and data
  constant: a 25-character observation name with an 82-character dataset name
  was accepted (it reached IPA's allowance check); a 25-character *dataset*
  name with an 82-character observation name was rejected. So the limit is
  specific to the observation — a long dataset name is fine.

  Names are now brought under 60 characters automatically, removing what
  carries the least meaning first. Two parts of a pipeline filename matter —
  the contrast (`Estrus_vs_2dpp`) and the cell type the comparison came from
  (`Immature_cortical_ovarian_stroma`) — and everything appended about how the
  pipeline ran does not.

  Paralome output is cut on its own structure rather than by heuristic. It
  names files
  `<contrast>_<celltype>_<method>_<test>_significant_<threshold>_<assay>`, so
  the `significant` literal anchors the cut exactly: drop it and everything
  after, drop the test immediately before it, drop the aggregation method. No
  list of test names is needed — the test is whatever token precedes the
  anchor, so `wilcox` works as well as `t`. Methods are matched as whole
  phrases in that one position only, since a cell type of `Naive_T_cell`
  shares both words with the `naive_cell` method.

  Files from anything else fall back to generic metadata removal, then any
  suffix the batch shares, then the shared prefix, and only as a last resort a
  two-ended cut marked with `..`.

  Comparison is token by token, so `Mature` is never read as a prefix of
  `Immature` and `cell_type` is never left as `cell_t`. Any step is abandoned
  if it would make two names identical or leave one unreadable, since
  observation names are what IPA lists side by side in a comparison analysis.
  Names already within the limit are untouched, and the dataset and analysis
  always keep the full filename.

- Rejection guidance now names the observation length first, since it is the
  cause hardest to guess from what IPA returns.

### Added

- **`--strip TEXT`**, repeatable, removes text from observation names before
  shortening — for a pipeline whose suffix the built-in list does not cover.
  Ignored as a whole if applying it would leave the names empty, unreadable, or
  no longer distinct.

### Notes

- This is a *second*, independent cause of the same misleading page — 1.1.0
  fixed duplicate dataset names. Having already attributed that page once made
  this one harder to see, not easier. If a batch dies on its first file with
  that wording, both causes are worth ruling out.

## 1.1.0 — 2026-08-05

### Added

- **Duplicate dataset names are detected before submitting.** IPA refuses a
  dataset whose name already exists in a project, and reports it as *"The page
  you are looking for is currently unavailable"* — wording that reads as an
  outage. Because dataset names come from filenames, re-running a batch retried
  names an earlier run had created, so the run died on its first file and
  looked like a total service failure. It cost a day to find.

  Established by experiment: the identical 2 KB request succeeded and then
  failed twice; with unique dataset names three consecutive submissions all
  succeeded. Not size, not rate limiting, not parameters, not an outage.

  `submit` now checks the submission log for that project and dataset name
  first, skips the file with an explanation, and files it under `submitted/`.
  `--force` overrides.

### Notes

- The guard covers submissions made through this tool with the same log file.
  A collision caused by another user or the IPA client still surfaces as the
  misleading outage page; the troubleshooting table now says so.

## 1.0.1 — 2026-08-05

### Fixed

- **An IPA outage is no longer reported as a bad parameter.** IPA's maintenance
  page ("currently unavailable", "experiencing technical difficulties", "try
  again later") is HTML, so it fell through to `MalformedRequestError` and the
  message told the user to check `--reference-set` and `--ID` — sending them to
  rewrite a command that was correct. New `ServiceUnavailableError`, also
  raised on 502/503/504, says plainly that IPA is down, that nothing about the
  command needs changing, and that re-running later resumes. Files are left in
  place, as before.

## 1.0.0 — 2026-08-05

First stable release. No code changes from 0.5.0 — the version marks that the
interface is settled and the package has been used in earnest.

Established in production: 37 analyses submitted against live IPA across 5
donors and 9 cell types, with batch submission, quota-aware resume and the
identifier/measurement vocabulary all exercised.

From here, breaking changes to the CLI or the Python API require a major
version bump.

### Interface considered stable

- `ipaapi validate | submit | status | report | history` and their flags.
- `ColumnMapping`, `Observation`, `Measurement`, `Dataset`, `IPAClient`.
- `MeasurementType`, `AnalysisStatus`, `ReferenceSet`, `GENE_ID_TYPES`.
- The exception hierarchy under `IPAError`.
- The submission log format (tab-separated, header row, append-only).

## 0.5.0 — 2026-08-05

### Changed

- **Released under the MIT licence.** `pyproject.toml` previously declared
  `Proprietary` with no `LICENSE` file present — a combination that, on a public
  repository, legally means nobody may use it. Adds `LICENSE`, the OSI
  classifier, and supported-Python classifiers.
- Package description rewritten to say what the tool does and what distinguishes
  it, and to stop advertising result retrieval, which needs a commercial add-on.
- README gains Contributing and Licence sections, and a Status section listing
  the known open questions rather than leaving them implicit.

## 0.4.1 — 2026-08-05

### Documentation

- **Extended README**, structured as a manual: quick start, full CLI reference,
  recipes, a section on the undocumented parts of IPA, authentication including
  headless use, Python API, troubleshooting table, and the wire format.
- **Corrected again — the reference-set size rule does not hold in practice.**
  0.4.0 reported §4.1.3.1's rule (ipkb below 2000 identifiers, dataset at 2000
  or more) as fact. Checked against 37 completed analyses of 1,804–6,245
  identifiers, *all* were scored against "Ingenuity Knowledge Base (Genes
  Only)". The documented rule is therefore not predictive; set
  `--reference-set` explicitly for anything you intend to compare.

## 0.4.0 — 2026-08-05

Working from QIAGEN's official *IPA Integration Module (APIs)* documentation
(April 2026) rather than from inference. Several things we had established by
trial were confirmed; two were wrong.

### Added

- **`GENE_ID_TYPES`** — all 33 documented `geneidtype` values with the database
  each refers to (§3.1), exposed as `ipaapi submit --list-id-types`. A value
  outside the list is warned about, with a near-match suggestion, but still
  sent; IPA remains the authority.
- **Species is carried by the identifier type**, not a separate parameter:
  `hugo` human, `mousesymeg` mouse, `ratsymeg` rat. There is no species
  argument in the API — an open question now closed.
- **`ReferenceSet.IPKB`** (`ipkb`), the Ingenuity Knowledge Base.

### Changed

- **Corrected: omitting `referenceset` does not mean "use the Knowledge
  Base".** IPA chooses by dataset size — `ipkb` below 2000 identifiers,
  `dataset` at 2000 or more (§4.1.3.1). Large pre-filtered hit lists therefore
  get their own genes as the background even with the parameter omitted. Pass
  `--reference-set ipkb` explicitly to override.

### Notes

- The measurement types and ranges this package enforces match §3.1 exactly.
- §3.1 also states that **out-of-range expression values are silently ignored**
  — "analysis will still proceed without errors or warning diagnostics", with
  offending entries dropped. That makes the range check load-bearing rather
  than pedantic: declaring log2 values as `foldchange` would have silently
  discarded every gene between -1 and 1.

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
