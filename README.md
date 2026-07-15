# jsPsych published papers

An automatically maintained dataset of academic papers that **cite or mention
[jsPsych](https://www.jspsych.org/)**, the JavaScript library for running
behavioral experiments in a web browser.

The data lives in two CSV files under [`data/`](data/) and is refreshed monthly
by a GitHub Actions workflow. No API keys are required to reproduce it.

## What's in the data

### `data/papers.csv`

One row per unique paper.

| column | meaning |
| --- | --- |
| `id` | Stable identifier. OpenAlex short ID (e.g. `W2897312833`) when the paper is known to OpenAlex, otherwise an Europe PMC-based id (`epmc:<SOURCE>:<id>`). |
| `doi` | Normalized lowercase DOI (bare, no `https://doi.org/` prefix), if known. |
| `title` | Paper title. |
| `venue` | Journal / source name. |
| `publication_year` | Year of publication. |
| `publication_date` | Publication date (`YYYY-MM-DD`) when known. |
| `type` | Work type (e.g. `article`, `preprint`). |
| `is_preprint` | **Derived, recomputed every run** — manual edits will not stick (unlike `notes`/`exclude`). `True` only when a strong preprint signal fires: `type` is exactly `preprint`, the DOI prefix belongs to a known preprint server (PsyArXiv, OSF Preprints, bioRxiv/medRxiv, arXiv, Research Square, Preprints.org, SocArXiv, EdArXiv, SSRN), or the venue names one of those servers. `False` means "not confidently identified as a preprint", **not** "confirmed published". |
| `duplicate_of` | **Derived, recomputed every run** — manual edits will not stick. When this row is a preprint (or earlier version) of another row in the dataset, the `id` of that canonical row; blank for canonical / unlinked rows. Chains are flattened (v1 → v2 → published becomes v1 → published), so a canonical row always has a blank `duplicate_of`. Rows are never deleted by linking — this column only annotates. See [Preprint → published linking](#preprint--published-linking). |
| `match_method` | **Derived** — which signal produced the link: `crossref`, `doi_version`, or `title_author` (see below). Blank whenever `duplicate_of` is blank. |
| `cited_by_count` | Citation count as reported by the source. |
| `cited_2015` | *How found* — cites the 2015 jsPsych paper (OpenAlex `W2161418887`). |
| `cited_joss` | *How found* — cites the 2023 jsPsych JOSS paper (OpenAlex `W4376138907`). |
| `fulltext_openalex` | *How found* — matches OpenAlex full-text search for `jsPsych`. |
| `fulltext_epmc` | *How found* — matches Europe PMC full-text search for `jspsych`. |
| `first_author` | Name of the first author. |
| `n_authors` | Number of authors. |
| `date_added` | UTC date the paper first entered this dataset. |
| `notes` | **Manual** free-text field (see below). |
| `exclude` | **Manual** flag (see below). |

A paper can be found by several sources at once, so more than one of the four
*how found* booleans may be `True` on a single row.

> **Note on boolean columns.** The four *how found* columns (and `exclude`)
> are stored as the literal strings `True` / `False`. Pandas parses these into
> proper booleans, but naive truthiness checks are a trap: in Python
> `bool("False")` is `True`, and in JavaScript `Boolean("False")` is `true`.
> When reading the CSV by hand, compare against the string `"True"` (e.g.
> `row["cited_2015"] == "True"`) or let pandas do the parsing.

### `data/authors.csv`

One row per author-paper pair.

| column | meaning |
| --- | --- |
| `paper_id` | Joins to `papers.csv.id`. |
| `doi` | DOI of the paper. |
| `publication_year` | Year of publication. |
| `author_position` | `first`, `middle`, or `last`. |
| `author_name` | Author display name. |
| `orcid` | ORCID iD, if known. |
| `institutions` | Semicolon-joined institution names. |
| `institution_countries` | Semicolon-joined ISO country codes (OpenAlex only). |
| `institution_rors` | Semicolon-joined [ROR](https://ror.org/) ids (OpenAlex only). |

The three institution columns are **positionally parallel lists**: the n-th
entry of each refers to the same institution. Empty slots are kept (e.g.
`US; ; GB`) so alignment survives institutions that lack a country code or
ROR id.

Europe PMC-only records provide free-text affiliation strings but no country
codes or ROR ids, so those two columns are empty for them.

## Data sources

| source | query | approx. hits |
| --- | --- | --- |
| OpenAlex — cites 2015 paper | `cites:W2161418887` | ~1,876 |
| OpenAlex — cites 2023 JOSS paper | `cites:W4376138907` | ~272 |
| OpenAlex — full-text mention | `fulltext.search:jsPsych` | ~2,028 |
| Europe PMC — full-text search | `"jspsych"` | ~747 |

All four are deduplicated into one set of papers. The dedupe key is the
lowercase DOI when present, falling back to the OpenAlex ID, with Europe
PMC-only records (no DOI matching any OpenAlex record) keyed by their
PMID/PMCID-based id. When a paper appears in more than one source, OpenAlex
metadata is preferred.

## Manual fields survive updates

`notes` and `exclude` in `papers.csv` are meant to be edited by hand — for
example to record why a hit is a false positive, or to mark it for exclusion
from downstream analysis. The update script uses **upsert** semantics:

- Existing rows are refreshed with the latest API metadata (affiliations in
  particular backfill over time in OpenAlex).
- `date_added`, `notes`, and `exclude` are **always preserved** from the
  existing file.
- Rows are **never deleted**, even if a paper drops out of the API results.

So it is safe to edit `notes` and `exclude` by hand; the monthly job will not
clobber them. (Author rows are fully replaced per paper on each run to pick up
affiliation backfill.)

## Preprint → published linking

Many works appear twice in the dataset: once as a preprint and once as the
published article (or as several numbered preprint versions). The update
script links these rows via the derived `duplicate_of` / `match_method`
columns so the analysis can count each *work* once. Only **very strong
signals** are used — a missed link merely leaves two rows counted separately,
whereas a wrong link silently merges two different papers, so precision is
prioritized over recall. Three tiers, strongest first (a stronger match is
never overwritten by a weaker one):

1. **`crossref`** — the preprint's own Crossref record carries an
   [`is-preprint-of`](https://www.crossref.org/documentation/schema-library/markup-guide-record-types/posted-content-includes-preprints/)
   relation whose target DOI exists in this dataset. This is
   publisher-asserted metadata, the strongest available signal. Results are
   cached in [`data/crossref_links.json`](data/crossref_links.json), which is
   committed to the repo: `links` maps normalized preprint DOI → published
   DOI (positive links, reused without re-querying), and `checked` records
   the date each preprint DOI was last queried. Negative results (404s,
   records without the relation) can turn positive later when publishers
   deposit the relation, so they are re-queried — but on a bounded schedule
   to keep the monthly run from growing forever: a negative is re-queried
   only when the preprint was published within the last **4 years** (recent
   preprints are the ones still getting published) or its last check is more
   than **365 days** old. A cached positive link whose target DOI is missing
   from the dataset falls back to a live query as if uncached. An old-style
   flat `{preprint_doi: published_doi}` cache file is migrated automatically
   on load.
2. **`doi_version`** — DOIs that differ only by a trailing `_vN` / `.vN`
   version suffix (e.g. `10.31234/osf.io/abcde_v1` / `..._v2`) are the same
   work by construction of the DOI itself. The canonical member is a
   non-preprint member when one exists, else the one with the latest
   publication date (ties: highest version number, then id).
3. **`title_author`** — a preprint and a research-type non-preprint row are
   linked only when **all** of the following hold: their normalized titles
   are identical (lowercased, punctuation collapsed) and the title is long
   enough to be distinctive (≥ 3 words and ≥ 15 characters); their first
   authors share the same normalized surname; and at least 50% of the smaller
   paper's author surnames appear on the other paper. A preprint matching
   more than one published candidate is left unlinked (ambiguous — logged,
   never guessed).

Chains are resolved transitively (v1 → v2 → published article collapses to
v1 → published article; the `match_method` still records the tier that linked
each row). Linking never deletes rows and, like `is_preprint`, both columns
are **recomputed from scratch every run** — manual edits to them will not
stick. `python update_papers.py --relink` reruns *only* this linking step
against the existing CSVs (Crossref is the only network dependency) and then
regenerates the analysis summaries, which is useful for testing without a
full refetch.

## Analysis

[`analysis/generate_summaries.py`](analysis/generate_summaries.py) (stdlib
only) derives two summary tables from the data CSVs:

- **`analysis/authors.csv`** — one row per unique author: `author_key`,
  `author_name`, `orcid`, `n_papers` (distinct works), `first_use` /
  `last_use` (min/max publication date of that author's works; falls back to
  the year when the full date is missing).
- **`analysis/institutions.csv`** — one row per unique institution:
  `institution_key`, `institution_name`, `ror`, `country`, `n_papers`
  (distinct works), `n_authors` (distinct author keys), `first_use`,
  `last_use`.
- **`analysis/journals.csv`** — one row per unique venue: `journal_key`,
  `journal_name`, `n_papers` (distinct works), `n_authors` (distinct author
  keys), `first_use`, `last_use`. Venues are keyed by normalized name
  (lowercased, runs of punctuation/whitespace collapsed to single spaces —
  this merges MEDLINE-style variants like *Journal of experimental
  psychology. General*) since the sources provide no ISSNs; `journal_name`
  shows the most frequent original spelling. A work's venue is its
  **canonical member's** venue, so a preprint linked to its published
  article counts for the journal rather than the preprint server — but
  works that exist *only* as preprints count under their server (PsyArXiv,
  bioRxiv, …), which is why preprint servers appear in the table. When the
  canonical member's venue is blank, it is inferred from the DOI registrant
  prefix (10.31234 → PsyArXiv, 10.31219 → OSF Preprints, 10.31235 →
  SocArXiv, 10.35542 → EdArXiv, 10.48550 → arXiv, 10.21203 → Research
  Square, 10.20944 → Preprints.org, 10.2139 → SSRN, 10.1101 →
  bioRxiv/medRxiv, the last shared between the two servers) — this affects
  only the journals table, never the raw data, and matters because OpenAlex
  leaves the venue blank on most PsyArXiv/OSF preprint records: PsyArXiv's
  count comes almost entirely from this fallback. A small alias map merges
  OpenAlex's alternate spellings of the same server into the fallback rows
  ("PsyArXiv (OSF Preprints)" → "PsyArXiv", "OSF Preprints (OSF Preprints)"
  → "OSF Preprints") so one server doesn't split into two rows; only
  observed splits are aliased, and *bioRxiv (Cold Spring Harbor
  Laboratory)* / *medRxiv* / *arXiv (Cornell University)* are left as-is
  (the joint "bioRxiv/medRxiv" fallback row can't be attributed to either
  server, and arXiv has no fallback row to merge). Works with no venue and
  no mapped DOI prefix are skipped (the script prints how many).

**Works, not rows.** Rows linked through `duplicate_of` (a preprint and its
published version, or several preprint versions) are collapsed into a single
*work* keyed by the canonical row's id. Each work counts once toward
`n_papers`; its authors and institutions are the union across its members;
its `first_use` is the earliest publication date among its non-excluded
members (so a preprint correctly marks when the work first appeared) and its
`last_use` the latest. Members with `exclude` set to `True` (or a
non-research type) are skipped individually, and a work whose members are
*all* excluded disappears from the analysis entirely. The summary printed by
the script reports how many analyzed works have more than one linked member.

**Dedup rules and their limits.** Authors are keyed by ORCID when present,
otherwise by normalized name (lowercase, whitespace collapsed, periods
stripped); when an ORCID group has several name spellings the most frequent
one is shown. Because ORCID coverage is inconsistent in the sources, a
name-only group is **merged into an ORCID group when its normalized name
unambiguously matches exactly one ORCID** seen in the data; it stays a
separate row when the name matches two or more different ORCIDs (ambiguous)
or is genuinely never seen with an ORCID. Institutions are keyed by ROR id
when present, else normalized name, with the same unambiguous-match merge.
Even so, name-only matching cannot distinguish two different people (or
institutions) that share an identical name, nor unify variant spellings of
the same one — so counts for records lacking identifiers are approximate.
Note also that Europe PMC affiliations are free text and noisy: they can
contain department strings, addresses, and embedded semicolons; obvious
email fragments are dropped during aggregation, but other free-text variants
of the same institution may appear as separate `name:` rows.

Papers with `exclude` set to `True` in `data/papers.csv` are omitted from
both tables, and so are papers whose `type` is a non-research type
(`software`, `peer-review`, `paratext`, `erratum`, `dataset`) — the raw data
CSVs keep everything; only the analysis filters them. The whole `type`
string must equal one of those values, so compound Europe PMC types such as
`research-article; Journal Article` are never filtered. The tables are
**regenerated from scratch every monthly run** —
manual edits to files under `analysis/` will be overwritten. Notes and
exclusions belong in `data/papers.csv`, which is the only place manual edits
persist.

## Running locally

```bash
pip install -r requirements.txt
python update_papers.py
python analysis/generate_summaries.py
```

This reads any existing CSVs, fetches all four sources, and rewrites
`data/papers.csv` and `data/authors.csv`. A fresh run takes a few minutes and
prints a summary (per-source counts, new papers this run, totals, links per
method). `python update_papers.py --relink` skips all fetching, reruns only
the preprint-linking step against the existing CSVs, and regenerates the
analysis summaries. Unit tests: `python3 -m unittest discover tests`.

**Rate-limit behavior.** HTTP 429/5xx responses are retried with backoff, but
any single wait (including a server-sent `Retry-After`) is capped at **300
seconds**. If a server demands a longer wait (OpenAlex has been observed
requesting ~8 hours), the script aborts immediately with a clear error
instead of hanging — a failed run is safe because the CI commit step only
commits when the data actually changed.

## Monthly schedule

`.github/workflows/update.yml` runs on cron `0 6 3 * *` (06:00 UTC on the 3rd
of each month) and can also be triggered manually via **workflow_dispatch**.
It installs dependencies, runs the script, and commits any changes under
`data/` (guarded by `git diff --quiet`) with a message like
`Monthly update: N new papers`.

## Known coverage gaps

- **Paywalled / non-indexed full text.** Full-text search only covers what
  OpenAlex and Europe PMC have indexed. Much of the long tail visible in
  Google Scholar — theses, unindexed conference proceedings, paywalled PDFs
  whose text is not exposed — will not appear here.
- **OpenAlex affiliation lag.** Very recent papers often land in OpenAlex with
  incomplete author affiliations; these backfill over subsequent months, which
  is why author rows are refreshed on every run.
- **Name/DOI normalization.** Records without a DOI can occasionally escape
  dedupe if the same work exists in OpenAlex and Europe PMC with no shared DOI,
  producing a small number of near-duplicates.
