#!/usr/bin/env python3
"""Update the jsPsych published-papers dataset.

Fetches four sources (three OpenAlex filters + Europe PMC full-text search),
normalizes and dedupes them into a single set of papers, and writes/updates
data/papers.csv and data/authors.csv with upsert semantics.

Only third-party dependency: requests.
"""

import csv
import hashlib
import os
import sys
import time
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

MAILTO = "josh.deleeuw@gmail.com"

OPENALEX_BASE = "https://api.openalex.org/works"
EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# OpenAlex fields to request (keeps responses small).
OPENALEX_SELECT = ",".join([
    "id",
    "doi",
    "title",
    "display_name",
    "publication_year",
    "publication_date",
    "primary_location",
    "authorships",
    "cited_by_count",
    "type",
])

# The three OpenAlex queries and the boolean flag each one sets.
OPENALEX_QUERIES = [
    ("cites:W2161418887", "cited_2015"),      # cites the jsPsych 2015 BRM paper
    ("cites:W4376138907", "cited_joss"),      # cites the jsPsych 2023 JOSS paper
    ("fulltext.search:jsPsych", "fulltext_openalex"),  # full-text mention
]

EPMC_QUERY = '"jspsych"'

FLAG_COLUMNS = ["cited_2015", "cited_joss", "fulltext_openalex", "fulltext_epmc"]

PAPER_COLUMNS = [
    "id", "doi", "title", "venue", "publication_year", "publication_date",
    "type", "cited_by_count", "cited_2015", "cited_joss",
    "fulltext_openalex", "fulltext_epmc", "first_author", "n_authors",
    "date_added", "notes", "exclude",
]

AUTHOR_COLUMNS = [
    "paper_id", "doi", "publication_year", "author_position", "author_name",
    "orcid", "institutions", "institution_countries", "institution_rors",
]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PAPERS_CSV = os.path.join(DATA_DIR, "papers.csv")
AUTHORS_CSV = os.path.join(DATA_DIR, "authors.csv")

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# --------------------------------------------------------------------------- #
# HTTP helper with modest retry / backoff
# --------------------------------------------------------------------------- #

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"jspsych-published-papers/1.0 (mailto:{MAILTO})"})

MAX_RETRIES = 5


def http_get_json(url, params):
    """GET with retry/backoff on 429 and 5xx responses."""
    backoff = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=60)
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            print(f"  request error ({exc}); retry {attempt}/{MAX_RETRIES} "
                  f"in {backoff:.0f}s", file=sys.stderr)
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                resp.raise_for_status()
            wait = backoff
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            print(f"  HTTP {resp.status_code}; retry {attempt}/{MAX_RETRIES} "
                  f"in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            backoff *= 2
            continue

        # Other 4xx: fail loudly.
        resp.raise_for_status()

    raise RuntimeError(f"Exhausted retries for {url}")


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #

def norm_doi(doi):
    """Return a normalized lowercase bare DOI, or '' if none."""
    if not doi:
        return ""
    doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi


def short_openalex_id(oa_id):
    """Turn 'https://openalex.org/W123' into 'W123'."""
    if not oa_id:
        return ""
    return oa_id.rstrip("/").rsplit("/", 1)[-1]


def dedupe_key(doi, paper_id):
    """Canonical dedupe key: lowercase DOI if present else the paper id."""
    return doi if doi else paper_id


# --------------------------------------------------------------------------- #
# Source fetchers
# --------------------------------------------------------------------------- #

def fetch_openalex(filter_str):
    """Fetch all works for an OpenAlex filter using cursor pagination."""
    results = []
    cursor = "*"
    page = 0
    while cursor:
        params = {
            "filter": filter_str,
            "select": OPENALEX_SELECT,
            "per-page": 200,
            "cursor": cursor,
            "mailto": MAILTO,
        }
        data = http_get_json(OPENALEX_BASE, params)
        batch = data.get("results", [])
        results.extend(batch)
        page += 1
        cursor = data.get("meta", {}).get("next_cursor")
        if not batch:
            break
    return results


def fetch_epmc(query):
    """Fetch all Europe PMC hits for a query using cursorMark pagination."""
    results = []
    cursor = "*"
    while True:
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": 1000,
            "cursorMark": cursor,
        }
        data = http_get_json(EPMC_BASE, params)
        batch = data.get("resultList", {}).get("result", [])
        results.extend(batch)
        next_cursor = data.get("nextCursorMark")
        if not batch or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return results


# --------------------------------------------------------------------------- #
# Record builders
# --------------------------------------------------------------------------- #

def build_openalex_paper(work, flag):
    doi = norm_doi(work.get("doi"))
    paper_id = short_openalex_id(work.get("id"))
    venue = ""
    ploc = work.get("primary_location") or {}
    source = ploc.get("source") or {}
    if source:
        venue = source.get("display_name") or ""

    authorships = work.get("authorships") or []
    first_author = ""
    for a in authorships:
        if a.get("author_position") == "first":
            first_author = (a.get("author") or {}).get("display_name") or ""
            break
    if not first_author and authorships:
        first_author = (authorships[0].get("author") or {}).get("display_name") or ""

    paper = {
        "id": paper_id,
        "doi": doi,
        "title": (work.get("title") or work.get("display_name") or "").strip(),
        "venue": venue,
        "publication_year": work.get("publication_year") or "",
        "publication_date": work.get("publication_date") or "",
        "type": work.get("type") or "",
        "cited_by_count": work.get("cited_by_count") or 0,
        "cited_2015": False,
        "cited_joss": False,
        "fulltext_openalex": False,
        "fulltext_epmc": False,
        "first_author": first_author,
        "n_authors": len(authorships),
        "source": "openalex",
    }
    paper[flag] = True

    authors = []
    for a in authorships:
        author = a.get("author") or {}
        insts = a.get("institutions") or []
        inst_names = [i.get("display_name") or "" for i in insts]
        inst_countries = [i.get("country_code") or "" for i in insts]
        inst_rors = [short_ror(i.get("ror")) for i in insts]
        authors.append({
            "author_position": a.get("author_position") or "",
            "author_name": author.get("display_name") or "",
            "orcid": norm_orcid(author.get("orcid")),
            "institutions": "; ".join(n for n in inst_names if n),
            "institution_countries": "; ".join(c for c in inst_countries if c),
            "institution_rors": "; ".join(r for r in inst_rors if r),
        })

    return paper, authors


def short_ror(ror):
    if not ror:
        return ""
    return ror.rstrip("/").rsplit("/", 1)[-1]


def norm_orcid(orcid):
    if not orcid:
        return ""
    return orcid.rstrip("/").rsplit("/", 1)[-1]


def build_epmc_paper(rec):
    doi = norm_doi(rec.get("doi"))
    src = rec.get("source") or "EPMC"
    rec_id = (rec.get("id") or "").strip()
    title_raw = (rec.get("title") or "").strip()
    if rec_id:
        paper_id = f"epmc:{src}:{rec_id}"
    elif doi:
        # No EPMC id: fall back to a DOI-based id so records can't collide
        # on an empty-id key.
        paper_id = f"epmc:doi:{doi}"
    elif title_raw:
        digest = hashlib.sha1(title_raw.lower().encode("utf-8")).hexdigest()[:12]
        paper_id = f"epmc:title:{digest}"
    else:
        print("  WARNING: skipping Europe PMC record with no id, DOI, or title",
              file=sys.stderr)
        return None, None

    venue = ""
    jinfo = rec.get("journalInfo") or {}
    journal = jinfo.get("journal") or {}
    venue = journal.get("title") or ""

    pub_types = rec.get("pubTypeList", {}).get("pubType") or []
    ptype = "; ".join(pub_types) if isinstance(pub_types, list) else str(pub_types)

    authors_raw = (rec.get("authorList") or {}).get("author") or []
    first_author = ""
    if authors_raw:
        first_author = authors_raw[0].get("fullName") or ""

    paper = {
        "id": paper_id,
        "doi": doi,
        "title": (rec.get("title") or "").strip().rstrip("."),
        "venue": venue,
        "publication_year": rec.get("pubYear") or "",
        "publication_date": rec.get("firstPublicationDate") or "",
        "type": ptype,
        "cited_by_count": rec.get("citedByCount") or 0,
        "cited_2015": False,
        "cited_joss": False,
        "fulltext_openalex": False,
        "fulltext_epmc": True,
        "first_author": first_author,
        "n_authors": len(authors_raw),
        "source": "epmc",
    }

    authors = []
    n = len(authors_raw)
    for idx, a in enumerate(authors_raw):
        if n == 1:
            position = "first"
        elif idx == 0:
            position = "first"
        elif idx == n - 1:
            position = "last"
        else:
            position = "middle"
        orcid = ""
        aid = a.get("authorId") or {}
        if aid.get("type") == "ORCID":
            orcid = aid.get("value") or ""
        affs = []
        detail = (a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or []
        for d in detail:
            aff = (d.get("affiliation") or "").strip()
            if aff:
                affs.append(aff)
        if not affs and a.get("affiliation"):
            affs.append(a.get("affiliation").strip())
        authors.append({
            "author_position": position,
            "author_name": a.get("fullName") or "",
            "orcid": orcid,
            "institutions": "; ".join(affs),
            "institution_countries": "",
            "institution_rors": "",
        })

    return paper, authors


# --------------------------------------------------------------------------- #
# Dedupe / merge
# --------------------------------------------------------------------------- #

def merge_flags(existing, incoming):
    for f in FLAG_COLUMNS:
        if incoming.get(f):
            existing[f] = True


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Fetch all sources
    # ------------------------------------------------------------------ #
    source_counts = {}
    # papers keyed by dedupe key -> paper dict; authors_by_key -> list
    papers = {}
    authors_by_key = {}

    for filter_str, flag in OPENALEX_QUERIES:
        print(f"Fetching OpenAlex: {filter_str} ...")
        works = fetch_openalex(filter_str)
        source_counts[flag] = len(works)
        print(f"  {len(works)} works")
        for work in works:
            paper, authors = build_openalex_paper(work, flag)
            key = dedupe_key(paper["doi"], paper["id"])
            if key in papers:
                merge_flags(papers[key], paper)
                # Remember alternate ids that collapsed into this record so
                # metadata saved under an old id can still be found.
                if paper["id"] != papers[key]["id"]:
                    papers[key]["alias_ids"].add(paper["id"])
                # Keep richest author list (OpenAlex preferred, backfill if empty).
                if not authors_by_key.get(key) and authors:
                    authors_by_key[key] = authors
                # Refresh metadata with the latest (they should agree).
            else:
                paper["alias_ids"] = set()
                papers[key] = paper
                authors_by_key[key] = authors

    print(f"Fetching Europe PMC: {EPMC_QUERY} ...")
    epmc_recs = fetch_epmc(EPMC_QUERY)
    source_counts["fulltext_epmc"] = len(epmc_recs)
    print(f"  {len(epmc_recs)} hits")
    for rec in epmc_recs:
        paper, authors = build_epmc_paper(rec)
        if paper is None:
            continue
        key = dedupe_key(paper["doi"], paper["id"])
        if key in papers:
            # Existing (prefer OpenAlex metadata): only mark the EPMC flag.
            papers[key]["fulltext_epmc"] = True
            if paper["id"] != papers[key]["id"]:
                papers[key]["alias_ids"].add(paper["id"])
            # Backfill authors if OpenAlex gave none.
            if not authors_by_key.get(key) and authors:
                authors_by_key[key] = authors
        else:
            paper["alias_ids"] = set()
            papers[key] = paper
            authors_by_key[key] = authors

    # ------------------------------------------------------------------ #
    # Load existing CSV to preserve manual fields / date_added
    # ------------------------------------------------------------------ #
    # Index each existing row's metadata under ALL plausible keys — its
    # normalized DOI (if any) AND its id — so a paper whose dedupe key drifts
    # between runs (e.g. a DOI backfilled onto a record first captured without
    # one) still finds its saved date_added/notes/exclude.
    existing_rows = []
    existing_meta = {}  # lookup key (doi or id) -> shared meta dict per row
    if os.path.exists(PAPERS_CSV):
        with open(PAPERS_CSV, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                meta = {
                    "date_added": row.get("date_added") or TODAY,
                    "notes": row.get("notes") or "",
                    "exclude": row.get("exclude") or "False",
                }
                existing_rows.append(row)
                row_doi = norm_doi(row.get("doi"))
                row_id = (row.get("id") or "").strip()
                if row_doi:
                    existing_meta.setdefault(row_doi, meta)
                if row_id:
                    existing_meta.setdefault(row_id, meta)

    # ------------------------------------------------------------------ #
    # Build final rows
    # ------------------------------------------------------------------ #
    new_count = 0
    paper_rows = []
    for key, paper in papers.items():
        # Look up saved metadata by any key this record presents: DOI first,
        # then its id, then any alternate ids that merged into it.
        meta = existing_meta.get(paper["doi"]) if paper["doi"] else None
        if meta is None:
            meta = existing_meta.get(paper["id"])
        if meta is None:
            for alias in paper.get("alias_ids", ()):
                meta = existing_meta.get(alias)
                if meta is not None:
                    break
        if meta:
            date_added = meta["date_added"]
            notes = meta["notes"]
            exclude = meta["exclude"]
        else:
            date_added = TODAY
            notes = ""
            exclude = "False"
            new_count += 1
        row = {
            "id": paper["id"],
            "doi": paper["doi"],
            "title": paper["title"],
            "venue": paper["venue"],
            "publication_year": paper["publication_year"],
            "publication_date": paper["publication_date"],
            "type": paper["type"],
            "cited_by_count": paper["cited_by_count"],
            "cited_2015": str(bool(paper["cited_2015"])),
            "cited_joss": str(bool(paper["cited_joss"])),
            "fulltext_openalex": str(bool(paper["fulltext_openalex"])),
            "fulltext_epmc": str(bool(paper["fulltext_epmc"])),
            "first_author": paper["first_author"],
            "n_authors": paper["n_authors"],
            "date_added": date_added,
            "notes": notes,
            "exclude": exclude,
        }
        paper_rows.append(row)

    # Preserve rows that disappeared from API results (never delete), but skip
    # any old row whose id OR doi is already claimed by a current-run row so
    # re-keyed papers don't survive as stale duplicates.
    claimed_ids = set()
    claimed_dois = set()
    for paper in papers.values():
        claimed_ids.add(paper["id"])
        claimed_ids.update(paper.get("alias_ids", ()))
        if paper["doi"]:
            claimed_dois.add(paper["doi"])
    preserved_ids = set()
    for old in existing_rows:
        old_id = (old.get("id") or "").strip()
        old_doi = norm_doi(old.get("doi"))
        if old_id in claimed_ids or (old_doi and old_doi in claimed_dois):
            continue
        # Ensure all columns present.
        row = {c: old.get(c, "") for c in PAPER_COLUMNS}
        paper_rows.append(row)
        preserved_ids.add(old_id)

    # Sort: publication_date descending, then title ascending.
    paper_rows.sort(key=lambda r: (r["title"] or "").lower())
    paper_rows.sort(key=lambda r: r["publication_date"] or "", reverse=True)

    # ------------------------------------------------------------------ #
    # Author rows (replace all rows per paper each run)
    # ------------------------------------------------------------------ #
    author_rows = []
    for key, paper in papers.items():
        for a in authors_by_key.get(key, []):
            author_rows.append({
                "paper_id": paper["id"],
                "doi": paper["doi"],
                "publication_year": paper["publication_year"],
                "author_position": a["author_position"],
                "author_name": a["author_name"],
                "orcid": a["orcid"],
                "institutions": a["institutions"],
                "institution_countries": a["institution_countries"],
                "institution_rors": a["institution_rors"],
            })

    # Preserve author rows only for papers that were themselves preserved
    # above (disappeared from API results but kept). Author rows for re-keyed
    # papers are not carried over — the fresh set replaces them.
    if os.path.exists(AUTHORS_CSV):
        with open(AUTHORS_CSV, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("paper_id") in preserved_ids:
                    author_rows.append({c: row.get(c, "") for c in AUTHOR_COLUMNS})

    # Sort authors by paper_id (stable: preserves author order within a paper).
    author_rows.sort(key=lambda r: r["paper_id"])

    # ------------------------------------------------------------------ #
    # Write CSVs
    # ------------------------------------------------------------------ #
    with open(PAPERS_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PAPER_COLUMNS)
        writer.writeheader()
        writer.writerows(paper_rows)

    with open(AUTHORS_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=AUTHOR_COLUMNS)
        writer.writeheader()
        writer.writerows(author_rows)

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    summary_lines = [
        "jsPsych published-papers update summary",
        f"  Date (UTC): {TODAY}",
        "  Source hit counts:",
        f"    cited_2015 (OpenAlex cites W2161418887): {source_counts.get('cited_2015', 0)}",
        f"    cited_joss (OpenAlex cites W4376138907): {source_counts.get('cited_joss', 0)}",
        f"    fulltext_openalex (OpenAlex fulltext):   {source_counts.get('fulltext_openalex', 0)}",
        f"    fulltext_epmc (Europe PMC):              {source_counts.get('fulltext_epmc', 0)}",
        f"  New papers this run: {new_count}",
        f"  Total papers: {len(paper_rows)}",
        f"  Total author rows: {len(author_rows)}",
    ]
    summary = "\n".join(summary_lines)
    print("\n" + summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")

    # Expose new-paper count for the workflow commit message.
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"new_papers={new_count}\n")


if __name__ == "__main__":
    main()
