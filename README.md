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

## Analysis

[`analysis/generate_summaries.py`](analysis/generate_summaries.py) (stdlib
only) derives two summary tables from the data CSVs:

- **`analysis/authors.csv`** — one row per unique author: `author_key`,
  `author_name`, `orcid`, `n_papers` (distinct papers), `first_use` /
  `last_use` (min/max publication date of that author's papers; falls back to
  the year when the full date is missing).
- **`analysis/institutions.csv`** — one row per unique institution:
  `institution_key`, `institution_name`, `ror`, `country`, `n_papers`
  (distinct papers), `n_authors` (distinct author keys), `first_use`,
  `last_use`.

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
both tables. The tables are **regenerated from scratch every monthly run** —
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
prints a summary (per-source counts, new papers this run, totals).

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
