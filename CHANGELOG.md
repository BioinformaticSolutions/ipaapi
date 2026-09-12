# Changelog

Versions follow [semantic versioning](https://semver.org): breaking changes to
the command line or the Python API bump the major number, additions bump the
minor, fixes bump the patch.

Check what you're running with `ipaapi --version`, which reports the version,
the install location, and whether it's an editable checkout rather than a wheel.

## 1.4.0 — 2026-09-12

Thirty defects, found by reading the package end to end three times rather than
by hitting them in use. The first pass found five; the second and third found
the rest, which is the argument for not stopping at the first handful. Every one
was reproduced before it was fixed, and each now has a regression test --
62 new tests, 265 in total.

Nothing here changes the command line or the Python API. What changes is what
the tool accepts, what it calls things, and what it reports.

### Fixed -- data reaching IPA

Five routes by which IPA accepted a submission, reported success, and scored an
analysis on something other than what was meant. That is the failure this
package exists to prevent.

- **The range check discarded every cell it could not parse before testing
  it.** `to_numeric(errors="coerce").dropna()`, then skip if empty -- so a
  `--FC` one column off validated clean and uploaded gene symbols as fold
  changes. It now counts cells that are neither numeric nor blank, quotes
  three, and says so explicitly when the whole column is unreadable, which is
  what an off-by-one looks like. Blank cells remain honest missing values.

- **Infinities passed every measurement type.** `inf >= 1` satisfies fold
  change, `logratio` and `other` are unbounded, and the upper bound of `ratio`
  and `intensity` is `inf` itself, so an ordinary DESeq2 or edgeR table -- which
  writes `Inf` wherever a group has zero counts -- cleared the check and had
  those rows dropped by IPA. Non-finite values are now rejected for every type,
  and the message names the cause.

- **The delimiter was sniffed from the header line alone.** A tab-separated
  file with a column called `log2FC, shrunken` sniffed as CSV and split into two
  columns whose names contained literal tabs -- which then passed the header
  check (two columns, no comment prefix) and the range check (the value column
  was entirely non-numeric, so it was skipped). Sniffing now scores each
  candidate on whether the header and the rows beneath it split into the *same*
  number of fields across eight lines. A real delimiter agrees; punctuation
  inside one cell does not.

- **A column label appearing twice in the frame shifted every value into the
  wrong slot.** `frame.loc[:, cols]` returns one column per match, so the row
  grew while `offset % n_slots` kept its period, and one observation received
  another's p-values as its fold changes. `validate()` checked the mapping for
  repeats but never the data.

- **`-` and `.` were sent as expression values.** Both are declared
  missing-value spellings and were honoured for identifiers, while the payload
  builder kept its own shorter list. They now share one vocabulary.

And four smaller ones in the same area: `gain_loss` and `classification` were
range-checked as continuous over (-2, 2) though IPA reads them as the codes -2,
-1, 0, 1, 2; a float-typed identifier column uploaded as `7157.0` and mapped to
nothing; cutoffs were rendered at six significant digits, rounding 1234567 to
`1.23457e+06`; and a `#` in front of a real header row -- bedtools, MACS -- was
rejected as a comment, with advice (`--skip-rows 1`) that then promoted the
first data row to be the header.

### Fixed -- misclassified errors

Quota and timeout both mean "the file is fine, leave it and stop", so a
per-file problem wearing either label halted the run, escaped quarantine, and
returned identically on every re-run.

- **The quota classifier matched the bare words "exceeded" and "limit
  reached"**, and it is tested first, so it beat every other branch. "Maximum
  upload size exceeded" and "Observation name length exceeded the maximum
  permitted" -- both permanent, both about the file -- were reported as an
  exhausted account. Those words now count only alongside something naming the
  allowance; the confirmed wording and HTTP 429 still match on their own.

- **A bare 502 or 504 anywhere in the body read as a gateway timeout**, so
  "Unable to run analysis: 504 identifiers could not be mapped" came back
  saying the command and the data were almost certainly fine. The numeric
  alternative is now anchored to the shapes a status code appears in.

- **`status()` accepted any HTTP 200 body.** IPA delivers its errors inside
  HTTP 200 and `from_code` maps anything that is not 3, 4 or 5 to
  `IN_PROGRESS`, so an error page read as "still running": `wait_for` polled
  out its entire budget and reported `in_progress`. A body that is not a bare
  status code now raises and quotes what IPA said.

- **A timeout is no longer described as safe to re-run.**
  `GatewayTimeoutError` subclasses `ServiceUnavailableError` and collected the
  flat "nothing about your command needs changing". A timeout loses the answer,
  not necessarily the request, and no analysis ID comes back to log -- so the
  duplicate guard cannot see a dataset the attempt may have created. The run
  now says that, and says that a duplicate rejection on the re-run means the
  analysis exists.

### Fixed -- the CLI reporting success it did not have

- **`submit` exited 0 with files sitting in `failed/`.** Validation failures
  were quarantined but never reached the exit code, which is what a cron entry
  or a Snakemake rule reads. `--dry-run` had the same hole and exited 0 where
  `validate` on the same directory exited 1. Quarantined files now count in
  every branch, are listed by name, and are included in the "N of M failed"
  denominator, which previously excluded the files that had failed hardest.

- **An interrupted batch lost the IDs of analyses IPA had accepted.** The log
  was written once after the loop while files were filed away inside it, so
  Ctrl-C on file two left file one in `submitted/` -- skipped by any re-run --
  with its analysis ID only in the scrollback. It is now appended per file,
  before the file is filed and before the next submission is attempted.

- **Observation names could still collide after the final trim.** Every earlier
  shortening step is guarded for uniqueness; `trim_name` was applied
  unconditionally and drops the middle, so two files differing only in the
  middle collapsed onto one name as soon as a third, unrelated file in the
  directory defeated the shared-affix stripping. Two analyses sharing an
  `obs1name` is what breaks a comparison analysis. Collisions are now detected
  and separated, and only the names that collided are tagged.

- **`trim_name` could reduce a name to `..rep`**; the hard-cut fallback fired
  only when both ends were empty. **The cutoff-token pattern made the decimal
  point optional**, so `p53`, `q30` and any bare integer read as a cutoff and
  were dropped -- taking the dose (10 vs 100) out of the name. **`--recursive`
  descended into hidden directories**, submitting `.ipynb_checkpoints` copies
  as extra analyses. **`status` and `report` unwound on the first unknown ID**,
  leaving every ID after it unchecked. **`history --limit 0` meant "no
  limit"**, and negatives sliced from the front. **`--help` printed
  "(default: None)"** after help text that already stated a truthful default.

### Fixed -- hangs, races and lost state

- **`login()` hung forever despite its timeout.** The callback server was a
  single-threaded `HTTPServer` with no handler timeout, so one connection that
  never completed a request blocked the accept loop -- the real redirect was
  never processed, *and* `server.shutdown()` in the `finally` waits on
  `serve_forever`, so the `AuthenticationError` the timeout raised was never
  delivered and the process sat silent. A browser preconnecting to
  `localhost:8000` was enough. Now threaded, with a per-connection timeout;
  measured, a 2s timeout now returns in 2.2s. The authorization code is also
  recorded before the response page is written, so a tab closed at the wrong
  moment no longer discards a code that had already arrived.

- **Concurrent logins lost tokens and blamed the wrong thing.** The cache wrote
  through a fixed `<path>.tmp` shared by every process and did a read, modify
  and write with no lock: six simultaneous logins left three entries, and the
  handler told the user their cache location was unwritable. The whole
  read-modify-write is now under an exclusive lock, through a unique temp file.

- **The cache was world-readable while the token was in it.** `open(tmp, "w")`
  creates at 0644 under a normal umask and the chmod to 0600 came after the
  write. It is now created 0600.

- **One non-UTF-8 byte in the log blocked `submit` entirely.**
  `UnicodeDecodeError` is a `ValueError` and slipped past the `OSError` guard,
  and `_already_submitted` reads the log for every dataset -- so a row written
  by Excel's tab-delimited export on Windows stopped the run. Reads now replace
  undecodable bytes and treat a damaged log as empty.

- **A truncated final row crashed `ipaapi history`**, because `DictReader`
  fills missing keys with `None` and `.get(key, "")` returns it. **Timestamps
  were written with a local offset** and compared lexicographically, so
  `--since` returned the wrong rows across a clock change, or permanently when
  two machines in different zones share a log; they are now UTC.
  **`Triage.summary()` counted moves that failed**, printing "2 file(s) moved
  to submitted/" directly beneath the warnings saying they had not been.
  **An error note could overwrite an existing one.** **A corrupt `expires_at`
  raised instead of reading as a cache miss.**

- **The missing-dependency message named a `pip` that may not exist.** It now
  names the running interpreter via `sys.executable`, and the sibling raise on
  the refresh path -- which offered no remedy at all -- says the same thing.
  `GatewayTimeoutError` is exported from `errors.__all__`.

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
