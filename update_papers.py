#!/usr/bin/env python3
"""Update the jsPsych published-papers dataset.

Fetches four sources (three OpenAlex filters + Europe PMC full-text search),
normalizes and dedupes them into a single set of papers, and writes/updates
data/papers.csv and data/authors.csv with upsert semantics.

Only third-party dependency: requests.
"""

import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

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
    "primary_topic",
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
    "type", "topic_field", "topic_subfield", "is_preprint", "duplicate_of",
    "match_method", "cited_by_count",
    "cited_2015", "cited_joss", "fulltext_openalex", "fulltext_epmc",
    "first_author", "n_authors", "date_added", "notes", "exclude",
]

# --------------------------------------------------------------------------- #
# is_preprint detection (high precision)
#
# The is_preprint column is DERIVED: it is recomputed from type/DOI/venue on
# every run, so manual edits to it will NOT stick (unlike notes/exclude).
# "True" means a strong preprint signal fired; "False" means "not confidently
# identified as a preprint", NOT "confirmed published".
# --------------------------------------------------------------------------- #

# DOI registrant prefixes belonging to preprint servers.
PREPRINT_DOI_PREFIXES = {
    "10.31234",  # PsyArXiv
    "10.31219",  # OSF Preprints
    "10.1101",   # bioRxiv / medRxiv
    "10.48550",  # arXiv
    "10.21203",  # Research Square
    "10.20944",  # Preprints.org
    "10.31235",  # SocArXiv
    "10.35542",  # EdArXiv
    "10.2139",   # SSRN
}

# Substrings that identify a preprint server in a venue name. "arxiv" as a
# bare containment is safe: every *arxiv-named venue is a preprint server.
# "osf preprints" is deliberately NOT listed: OpenAlex assigns that venue to
# some OSF-hosted materials that are not manuscripts, so the venue alone is
# not a high-certainty signal (true OSF preprints are still caught by the
# 10.31219 DOI prefix or type == "preprint").
PREPRINT_VENUE_TOKENS = [
    "psyarxiv", "biorxiv", "medrxiv", "arxiv", "ssrn", "research square",
    "preprints.org",
]


def compute_is_preprint(work_type, doi, venue):
    """True only when a strong preprint signal fires (see notes above)."""
    if (work_type or "").strip().lower() == "preprint":
        return True
    if doi and doi.split("/", 1)[0] in PREPRINT_DOI_PREFIXES:
        return True
    venue_l = (venue or "").lower()
    if venue_l and any(tok in venue_l for tok in PREPRINT_VENUE_TOKENS):
        return True
    return False

def extract_topic(work):
    """(topic_field, topic_subfield) display names from an OpenAlex work's
    primary_topic. Both are '' when the work has no primary_topic (always
    the case for Europe PMC-only records, and for some OpenAlex records).
    OpenAlex assigns topics algorithmically, so occasional misassignments
    are expected."""
    pt = work.get("primary_topic") or {}
    field = (pt.get("field") or {}).get("display_name") or ""
    subfield = (pt.get("subfield") or {}).get("display_name") or ""
    return field, subfield


AUTHOR_COLUMNS = [
    "paper_id", "doi", "publication_year", "author_position", "author_name",
    "orcid", "institutions", "institution_countries", "institution_rors",
]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PAPERS_CSV = os.path.join(DATA_DIR, "papers.csv")
AUTHORS_CSV = os.path.join(DATA_DIR, "authors.csv")
CROSSREF_LINKS_JSON = os.path.join(DATA_DIR, "crossref_links.json")

# Non-research work types (kept in sync with analysis/generate_summaries.py).
# Used by the title_author linking tier: a preprint may only be linked to a
# published candidate whose whole `type` is NOT one of these.
NON_RESEARCH_TYPES = {"software", "peer-review", "paratext", "erratum", "dataset"}

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# --------------------------------------------------------------------------- #
# HTTP helper with modest retry / backoff
# --------------------------------------------------------------------------- #

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"jspsych-published-papers/1.0 (mailto:{MAILTO})"})

MAX_RETRIES = 5

# Hard ceiling on any single sleep we will honor before retrying. If a server
# asks us to wait longer than this (e.g. an 8-hour OpenAlex rate-limit
# Retry-After), we abort loudly instead of hanging the run for hours: a failed
# run is safe (the CI commit step is guarded by `git diff --quiet`), a silent
# multi-hour hang is not.
MAX_WAIT_SECONDS = 300


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
            # Parse under the retry umbrella: a 200 with a non-JSON body
            # (CDN interstitial, upstream hiccup) is transient — retry it
            # instead of killing a multi-hundred-request run.
            try:
                return resp.json()
            except ValueError as exc:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"non-JSON 200 response from {url}: {exc}")
                print(f"  non-JSON 200 body ({exc}); retry "
                      f"{attempt}/{MAX_RETRIES} in {backoff:.0f}s",
                      file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                resp.raise_for_status()
            wait = backoff
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    ra = float(retry_after)
                except ValueError:
                    ra = None  # HTTP-date form; fall back to our own backoff
                if ra is not None:
                    if ra > MAX_WAIT_SECONDS:
                        raise RuntimeError(
                            f"rate limited: server requested {ra:.0f}s wait; "
                            f"aborting (cap is {MAX_WAIT_SECONDS}s)")
                    wait = max(wait, ra)
            wait = min(wait, MAX_WAIT_SECONDS)
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
        "topic_field": extract_topic(work)[0],
        "topic_subfield": extract_topic(work)[1],
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
            # Join WITHOUT dropping empty entries so the three columns stay
            # positionally parallel (an institution may lack a country/ROR).
            "institutions": join_parallel(inst_names),
            "institution_countries": join_parallel(inst_countries),
            "institution_rors": join_parallel(inst_rors),
        })

    return paper, authors


def join_parallel(items):
    """Semicolon-join a parallel list, keeping empty slots for alignment."""
    return "; ".join(items) if any(items) else ""


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
        "topic_field": "",     # Europe PMC provides no OpenAlex topics
        "topic_subfield": "",
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


# --------------------------------------------------------------------------- #
# Preprint -> published-version linking
#
# Populates two DERIVED columns on each paper row (recomputed every run, so
# manual edits will NOT stick):
#   duplicate_of  id of the canonical row this row duplicates ('' if canonical)
#   match_method  "crossref" | "doi_version" | "title_author" ('' if canonical)
#
# Three tiers, strongest first; a stronger match is never overwritten by a
# weaker one. Rows are never deleted; linking only annotates.
# --------------------------------------------------------------------------- #

# Strength ordering (higher = stronger). Tiers are processed strongest-first
# and a row already assigned by a stronger tier is skipped by weaker ones.
_TIER_STRENGTH = {"crossref": 3, "doi_version": 2, "title_author": 1}

# Trailing DOI version suffix: "_v3" or ".v3".
_DOI_VERSION_RE = re.compile(r"^(.*?)[._]v(\d+)$")


def norm_title(title):
    """Lowercase, collapse non-alphanumeric runs to single spaces, trim."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def surname(name):
    """Normalized surname: last whitespace token, lowercased ('' if none)."""
    parts = (name or "").strip().lower().split()
    return parts[-1] if parts else ""


def build_author_info(author_rows):
    """paper_id -> {'first_surname': str, 'surnames': set(str)}.

    First-author surname comes from the `first`-position row when present,
    else the first author row seen for the paper (authors.csv keeps author
    order within a paper).
    """
    info = {}
    for row in author_rows:
        pid = row.get("paper_id")
        if not pid:
            continue
        sn = surname(row.get("author_name"))
        d = info.setdefault(
            pid, {"first_surname": "", "surnames": set(), "_fallback": ""})
        if sn:
            d["surnames"].add(sn)
        if not d["_fallback"] and sn:
            d["_fallback"] = sn
        if (row.get("author_position") or "").strip() == "first" \
                and not d["first_surname"]:
            d["first_surname"] = sn
    for d in info.values():
        if not d["first_surname"]:
            d["first_surname"] = d["_fallback"]
    return info


def _is_preprint(row):
    return str(row.get("is_preprint")) == "True"


# Re-query policy for negative (no-link-found) Crossref results. Without a
# bound, every never-linked preprint would be re-queried monthly forever.
# Preprints published within the last CROSSREF_RECHECK_PUB_YEARS are still
# likely to get published, so they are re-checked on every run; older ones
# are re-checked only when their last check is over CROSSREF_RECHECK_DAYS old.
CROSSREF_RECHECK_PUB_YEARS = 4
CROSSREF_RECHECK_DAYS = 365


def _load_crossref_cache(path):
    """Load the Crossref link cache.

    New format: {"links": {preprint_doi: published_doi},
                 "checked": {preprint_doi: "YYYY-MM-DD"}}   (last query date)
    A legacy flat {preprint_doi: published_doi} mapping is migrated on load
    (treated as links-only, no check dates).
    """
    links, checked = {}, {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                if "links" in data or "checked" in data:
                    raw_links = data.get("links") or {}
                    raw_checked = data.get("checked") or {}
                else:  # legacy flat format
                    raw_links, raw_checked = data, {}
                links = {norm_doi(k): norm_doi(v)
                         for k, v in raw_links.items() if isinstance(v, str)}
                checked = {norm_doi(k): v
                           for k, v in raw_checked.items() if isinstance(v, str)}
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  WARNING: could not read {path}: {exc}", file=sys.stderr)
    return {"links": links, "checked": checked}


def _save_crossref_cache(path, cache):
    payload = {
        "links": dict(sorted(cache["links"].items())),
        "checked": dict(sorted(cache["checked"].items())),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _should_requery_negative(pub_date, last_checked, today=None):
    """Whether a previously-negative preprint DOI should be re-queried.

    Never-checked DOIs are always queried. Otherwise re-query when the
    preprint was published within the last CROSSREF_RECHECK_PUB_YEARS
    (recent preprints get published), OR the last check is more than
    CROSSREF_RECHECK_DAYS old.
    """
    if not last_checked:
        return True
    today_dt = datetime.strptime(today or TODAY, "%Y-%m-%d")
    try:
        cutoff_dt = today_dt.replace(year=today_dt.year - CROSSREF_RECHECK_PUB_YEARS)
    except ValueError:  # Feb 29 in a non-leap target year
        cutoff_dt = today_dt.replace(
            year=today_dt.year - CROSSREF_RECHECK_PUB_YEARS, day=28)
    pub_cutoff = cutoff_dt.strftime("%Y-%m-%d")
    if (pub_date or "") >= pub_cutoff:
        return True
    try:
        age = (today_dt - datetime.strptime(last_checked, "%Y-%m-%d")).days
    except ValueError:
        return True  # unparseable check date: re-query and rewrite it
    return age > CROSSREF_RECHECK_DAYS


def crossref_get_work(doi):
    """Fetch a Crossref work record's `message`, or None on 404/missing."""
    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    try:
        data = http_get_json(url, {"mailto": MAILTO})
    except requests.HTTPError as exc:
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code == 404:
            return None
        raise
    return (data or {}).get("message")


def _choose_canonical(rows):
    """Pick the canonical row: non-preprint first, then latest date, then id."""
    ordered = sorted(rows, key=lambda r: r["id"])
    ordered.sort(key=lambda r: (r.get("publication_date") or ""), reverse=True)
    ordered.sort(key=lambda r: 0 if not _is_preprint(r) else 1)
    return ordered[0]


def link_papers(paper_rows, author_rows, cache_path=CROSSREF_LINKS_JSON,
                use_crossref=True):
    """Assign duplicate_of / match_method across paper_rows. Returns stats."""
    stats = {
        "crossref_links": 0, "crossref_queries": 0, "crossref_cache_hits": 0,
        "crossref_new_cache": 0, "crossref_skipped_policy": 0,
        "doi_version_links": 0,
        "title_author_links": 0, "title_author_ambiguous": 0,
        "cycles_broken": 0,
    }

    for r in paper_rows:
        r["duplicate_of"] = ""
        r["match_method"] = ""

    by_id = {r["id"]: r for r in paper_rows}
    doi_to_row = {}
    for r in paper_rows:
        d = norm_doi(r.get("doi"))
        if d:
            doi_to_row.setdefault(d, r)  # first row wins on duplicate DOIs

    author_info = build_author_info(author_rows)

    # child_id -> (canonical_id, method). Only ever set once (strongest tier
    # that reaches a row wins, since we process tiers strongest-first).
    assign = {}

    def try_assign(child_id, canonical_id, method):
        if child_id == canonical_id or child_id in assign:
            return False
        assign[child_id] = (canonical_id, method)
        return True

    # ---- Tier 1: crossref is-preprint-of ------------------------------- #
    cache = _load_crossref_cache(cache_path)
    links, checked = cache["links"], cache["checked"]
    for r in paper_rows:
        if not _is_preprint(r):
            continue
        pdoi = norm_doi(r.get("doi"))
        if not pdoi:
            continue
        target = None
        cached_tdoi = links.get(pdoi)
        if cached_tdoi and cached_tdoi in doi_to_row:
            stats["crossref_cache_hits"] += 1
            target = doi_to_row[cached_tdoi]
        elif use_crossref:
            # No usable cached link. When a cached link exists but its target
            # DOI is absent from the dataset, query live as if uncached (the
            # relation may have changed). Otherwise this DOI is uncached or a
            # previous negative: consult the re-query policy so old, stale
            # negatives aren't re-queried on every monthly run.
            if cached_tdoi is None and not _should_requery_negative(
                    (r.get("publication_date") or "").strip(),
                    checked.get(pdoi)):
                stats["crossref_skipped_policy"] += 1
                continue
            stats["crossref_queries"] += 1
            msg = crossref_get_work(pdoi)
            checked[pdoi] = TODAY  # record every live query's date
            if msg:
                rels = (msg.get("relation") or {}).get("is-preprint-of") or []
                for rel in rels:
                    if rel.get("id-type") != "doi":
                        continue
                    tdoi = norm_doi(rel.get("id"))
                    if tdoi and tdoi in doi_to_row:
                        # Cache the positive link. Negatives (404s /
                        # no-relation results) are recorded only in `checked`
                        # so they can turn positive on a later re-query.
                        links[pdoi] = tdoi
                        stats["crossref_new_cache"] += 1
                        target = doi_to_row[tdoi]
                        break
        if target is not None and try_assign(r["id"], target["id"], "crossref"):
            stats["crossref_links"] += 1
    _save_crossref_cache(cache_path, cache)

    # ---- Tier 2: DOI version families (_vN / .vN) ---------------------- #
    families = {}
    for r in paper_rows:
        d = norm_doi(r.get("doi"))
        if not d:
            continue
        m = _DOI_VERSION_RE.match(d)
        base, ver = (m.group(1), int(m.group(2))) if m else (d, 0)
        families.setdefault(base, []).append((r, ver))
    for base, members in families.items():
        if len(members) < 2 or not any(v > 0 for _, v in members):
            continue
        nonpre = [(r, v) for r, v in members if not _is_preprint(r)]
        pool = nonpre if nonpre else members
        pool = sorted(pool, key=lambda it: it[0]["id"])
        pool.sort(key=lambda it: ((it[0].get("publication_date") or ""), it[1]),
                  reverse=True)
        canonical = pool[0][0]
        for r, _ in members:
            if r is canonical:
                continue
            if try_assign(r["id"], canonical["id"], "doi_version"):
                stats["doi_version_links"] += 1

    # ---- Tier 3: identical title + first-author + author overlap ------- #
    def eligible_title(row):
        nt = norm_title(row.get("title"))
        return nt if (len(nt) >= 15 and len(nt.split()) >= 3) else None

    pub_by_title = {}
    for r in paper_rows:
        if _is_preprint(r):
            continue
        if (r.get("type") or "").strip().lower() in NON_RESEARCH_TYPES:
            continue
        nt = eligible_title(r)
        if nt:
            pub_by_title.setdefault(nt, []).append(r)

    for r in paper_rows:
        if not _is_preprint(r):
            continue
        nt = eligible_title(r)
        if not nt:
            continue
        pinfo = author_info.get(r["id"])
        if not pinfo or not pinfo["first_surname"]:
            continue
        matches = []
        for c in pub_by_title.get(nt, []):
            cinfo = author_info.get(c["id"])
            if not cinfo or cinfo["first_surname"] != pinfo["first_surname"]:
                continue
            smaller = min(len(pinfo["surnames"]), len(cinfo["surnames"]))
            if smaller == 0:
                continue
            overlap = len(pinfo["surnames"] & cinfo["surnames"])
            if overlap / smaller < 0.5:
                continue
            matches.append(c)
        if len(matches) > 1:
            stats["title_author_ambiguous"] += 1
            print(f"  title_author ambiguous: preprint {r['id']} matches "
                  f"{len(matches)} published candidates; skipping",
                  file=sys.stderr)
            continue
        if len(matches) == 1 and try_assign(
                r["id"], matches[0]["id"], "title_author"):
            stats["title_author_links"] += 1

    # ---- Break any cycles (defensive; strong signals rarely cycle) ----- #
    parent = dict(assign)  # child_id -> (canonical_id, method)

    def break_one_cycle():
        safe = set()
        for start in list(parent):
            if start in safe:
                continue
            walk, pos, cur = [], {}, start
            while True:
                if cur not in parent or cur in safe:
                    safe.update(walk)
                    break
                if cur in pos:  # found a cycle
                    cyc = walk[pos[cur]:]
                    best = _choose_canonical([by_id[n] for n in cyc])
                    parent.pop(best["id"], None)
                    print(f"  cycle broken among {cyc}; canonical -> "
                          f"{best['id']}", file=sys.stderr)
                    stats["cycles_broken"] += 1
                    return True
                pos[cur] = len(walk)
                walk.append(cur)
                cur = parent[cur][0]
        return False

    while break_one_cycle():
        pass

    # ---- Flatten chains: every child points at its terminal canonical -- #
    def terminal(node):
        cur = node
        while cur in parent:
            cur = parent[cur][0]
        return cur

    for child, (_, method) in parent.items():
        root = terminal(child)
        if root != child:
            by_id[child]["duplicate_of"] = root
            by_id[child]["match_method"] = method

    # ---- Safety check: no published row points at a preprint via the ---- #
    # crossref/title_author tiers (only doi_version may, for all-preprint     #
    # families).                                                              #
    for child, (_, method) in parent.items():
        crow = by_id[child]
        if not _is_preprint(crow) and method in ("crossref", "title_author"):
            print(f"  WARNING: published row {child} linked to preprint via "
                  f"{method}; this should not happen", file=sys.stderr)

    return stats


def load_paper_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for c in PAPER_COLUMNS:
            r.setdefault(c, "")
    return rows


def load_author_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_paper_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PAPER_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in PAPER_COLUMNS})


def relink():
    """Run ONLY the linking step against the existing data CSVs.

    Loads data/papers.csv and data/authors.csv, recomputes is_preprint and the
    duplicate_of / match_method columns, and rewrites data/papers.csv. Makes no
    OpenAlex/Europe PMC calls (Crossref only), so it is safe to run when those
    APIs are rate-limiting. authors.csv is not modified. The analysis
    summaries are regenerated afterward so they never go stale.
    """
    print("Relinking from existing CSVs (no OpenAlex/EPMC fetch) ...")
    paper_rows = load_paper_rows(PAPERS_CSV)
    author_rows = load_author_rows(AUTHORS_CSV)
    # is_preprint is derived; recompute so linking sees current values.
    for r in paper_rows:
        r["is_preprint"] = str(compute_is_preprint(
            r.get("type"), norm_doi(r.get("doi")), r.get("venue")))
    stats = link_papers(paper_rows, author_rows)
    write_paper_rows(PAPERS_CSV, paper_rows)
    _print_link_stats(stats, len(paper_rows))
    # Regenerate the analysis summaries so they reflect the new links.
    summaries = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "analysis", "generate_summaries.py")
    print("\nRegenerating analysis summaries ...")
    subprocess.run([sys.executable, summaries], check=True)


def _print_link_stats(stats, n_papers):
    linked = (stats["crossref_links"] + stats["doi_version_links"]
              + stats["title_author_links"])
    print("Linking summary")
    print(f"  Total papers: {n_papers}")
    print(f"  Links (crossref):     {stats['crossref_links']} "
          f"(queries {stats['crossref_queries']}, "
          f"cache hits {stats['crossref_cache_hits']}, "
          f"new cache {stats['crossref_new_cache']}, "
          f"negatives skipped by policy {stats['crossref_skipped_policy']})")
    print(f"  Links (doi_version):  {stats['doi_version_links']}")
    print(f"  Links (title_author): {stats['title_author_links']} "
          f"(ambiguous skipped {stats['title_author_ambiguous']})")
    print(f"  Cycles broken: {stats['cycles_broken']}")
    print(f"  Total duplicate rows linked: {linked}")


def main():
    if "--relink" in sys.argv:
        relink()
        return
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
            "topic_field": paper.get("topic_field", ""),
            "topic_subfield": paper.get("topic_subfield", ""),
            "is_preprint": str(compute_is_preprint(
                paper["type"], paper["doi"], paper["venue"])),
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
        # is_preprint is derived, so recompute it even for preserved rows
        # (also backfills rows written before the column existed).
        row["is_preprint"] = str(compute_is_preprint(
            old.get("type"), norm_doi(old.get("doi")), old.get("venue")))
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
    # Link preprints to their published versions (derived columns). Runs
    # here so the monthly CI job does it too; --relink reruns just this step.
    # ------------------------------------------------------------------ #
    link_stats = link_papers(paper_rows, author_rows)
    _print_link_stats(link_stats, len(paper_rows))

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
        "  Preprint->published links:",
        f"    crossref:     {link_stats['crossref_links']}",
        f"    doi_version:  {link_stats['doi_version_links']}",
        f"    title_author: {link_stats['title_author_links']} "
        f"(ambiguous skipped {link_stats['title_author_ambiguous']})",
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
