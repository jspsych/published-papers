#!/usr/bin/env python3
"""Find open materials (experiment code) for papers in data/papers.csv.

Three stages, all deterministic (no LLM):

1. Europe PMC full-text mining: for papers with full text in Europe PMC,
   fetch the JATS XML and regex-extract repository links (OSF, GitHub,
   GitLab, Zenodo, Pavlovia, Gorilla), recording which section each link
   came from (data-availability statement > body > references).
2. OSF preprint lookup: for OSF-hosted preprints (PsyArXiv, OSF Preprints,
   EdArXiv, SocArXiv), ask the OSF API for the preprint's supplemental
   project node directly — no text mining needed.
3. Validation: for OSF nodes/registrations, GitHub repos, and Zenodo
   records, list files and look for jsPsych markers (a "jspsych" filename,
   or "jsPsych" inside small .html/.js files) to set jspsych_confirmed.

Results go to data/materials.csv (one row per paper-link pair). Progress is
cached in data/materials_cache.json so runs are resumable and the monthly
job only processes new papers.

Environment:
  OSF_TOKEN     optional OSF personal access token (unauthenticated OSF API
                calls are throttled hard; a token makes validation feasible)
  GITHUB_TOKEN  optional GitHub token (required for GitHub validation)

Usage:
  python find_materials.py                 # extract new papers, then validate
  python find_materials.py --limit 50      # cap papers extracted this run
  python find_materials.py --no-validate   # extraction only
  python find_materials.py --validate-only # skip extraction, just validate

Only third-party dependency: requests.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

MAILTO = "josh.deleeuw@gmail.com"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PAPERS_CSV = os.path.join(DATA_DIR, "papers.csv")
MATERIALS_CSV = os.path.join(DATA_DIR, "materials.csv")
CACHE_JSON = os.path.join(DATA_DIR, "materials_cache.json")

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
OSF_API = "https://api.osf.io/v2"
GITHUB_API = "https://api.github.com"
ZENODO_API = "https://zenodo.org/api/records"

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

MATERIALS_COLUMNS = [
    "paper_id", "doi", "url", "url_type", "source", "section",
    "jspsych_confirmed", "checked_date",
]

# Papers with no full text available are re-checked after this many days
# (full text often appears in Europe PMC months after publication).
NOFT_RECHECK_DAYS = 180

# DOI prefixes of OSF-hosted preprint servers (preprint GUID == DOI suffix).
OSF_PREPRINT_PREFIXES = ("10.31234/", "10.31219/", "10.35542/", "10.31235/")

# Per-repository budgets for validation.
OSF_MAX_REQUESTS_PER_NODE = 30
OSF_MAX_FILE_DOWNLOADS = 5
OSF_MAX_FILE_BYTES = 2_000_000
GH_MAX_FILE_DOWNLOADS = 5

# --------------------------------------------------------------------------- #
# Link extraction
# --------------------------------------------------------------------------- #

# osf.io path segments that are app routes, not GUIDs.
OSF_RESERVED = {
    "search", "login", "logout", "support", "register", "signup", "goodbye",
    "dashboard", "prereg", "institutions", "preprints", "registries",
    "meetings", "api", "help", "settings", "profile", "project", "view",
    "share", "donate", "activity", "explore", "quickfiles", "reviews",
}

# GitHub owners whose repos are the jsPsych library itself, not paper
# materials.
GITHUB_EXCLUDED_OWNERS = {"jspsych"}

_LINK_PATTERNS = [
    # (url_type, compiled regex). Group 1 (+2) capture the identifying parts.
    ("osf", re.compile(r"osf\.io/([a-z0-9]{4,7})\b", re.I)),
    ("github", re.compile(r"github\.com/([\w-]+)/([\w.-]+)", re.I)),
    ("gitlab", re.compile(r"gitlab\.com/([\w.-]+)/([\w.-]+)", re.I)),
    ("zenodo", re.compile(r"zenodo\.org/records?/(\d+)", re.I)),
    ("zenodo", re.compile(r"10\.5281/zenodo\.(\d+)", re.I)),
    ("pavlovia", re.compile(r"(?:gitlab\.)?pavlovia\.org/([\w.-]+)/([\w.-]+)", re.I)),
    ("gorilla", re.compile(r"gorilla\.sc/openmaterials/(\w+)", re.I)),
]

_TRAILING_JUNK = ".,;:)]}>\"'"


def _clean(token):
    token = token.rstrip(_TRAILING_JUNK)
    if token.lower().endswith(".git"):
        token = token[:-4]
    return token


def extract_links(text, own_guids=frozenset()):
    """Extract normalized repository links from text.

    Returns an ordered list of unique (url, url_type) tuples. `own_guids` is
    a set of lowercase OSF GUIDs to skip (e.g. GUIDs that are themselves
    preprint DOIs in the dataset — citations of papers, not materials).
    """
    seen = set()
    out = []

    def add(url, url_type):
        key = url.lower()
        if key not in seen:
            seen.add(key)
            out.append((url, url_type))

    for url_type, pattern in _LINK_PATTERNS:
        for m in pattern.finditer(text):
            if url_type == "osf":
                guid = m.group(1).lower()
                if guid in OSF_RESERVED or guid in own_guids:
                    continue
                add(f"https://osf.io/{guid}", "osf")
            elif url_type in ("github", "gitlab"):
                owner = _clean(m.group(1))
                repo = _clean(m.group(2))
                if not repo or repo.lower() in ("issues", "wiki", "blob", "tree"):
                    continue
                if url_type == "github" and owner.lower() in GITHUB_EXCLUDED_OWNERS:
                    continue
                add(f"https://{url_type}.com/{owner}/{repo}", url_type)
            elif url_type == "zenodo":
                add(f"https://zenodo.org/records/{m.group(1)}", "zenodo")
            elif url_type == "pavlovia":
                owner = _clean(m.group(1))
                repo = _clean(m.group(2))
                if not repo:
                    continue
                add(f"https://pavlovia.org/{owner}/{repo}", "pavlovia")
            elif url_type == "gorilla":
                add(f"https://app.gorilla.sc/openmaterials/{m.group(1)}", "gorilla")
    return out


# Section titles that indicate an availability / open-practices statement.
_AVAIL_TITLE_RE = re.compile(
    r"availab|open\s+(practices|science|data|materials|code)"
    r"|data\s+and\s+(code|materials)|code\s+and\s+data|accessib",
    re.I,
)
_AVAIL_SECTYPE_RE = re.compile(r"data-availability|availability", re.I)


def _elem_text(elem):
    return " ".join(elem.itertext())


def extract_links_with_sections(xml_text, own_guids=frozenset()):
    """Extract links from JATS XML, tagging each with the section it came
    from: 'data_availability' > 'body' > 'references'. Falls back to raw
    regex with section 'unknown' if the XML does not parse.

    Returns a list of (url, url_type, section) tuples, one per unique URL,
    keeping the strongest section for each.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [(u, t, "unknown") for u, t in extract_links(xml_text, own_guids)]

    avail_urls = {}
    for sec in root.iter("sec"):
        sec_type = sec.get("sec-type") or ""
        title_el = sec.find("title")
        title = _elem_text(title_el) if title_el is not None else ""
        if _AVAIL_SECTYPE_RE.search(sec_type) or _AVAIL_TITLE_RE.search(title):
            for u, t in extract_links(_elem_text(sec), own_guids):
                avail_urls.setdefault(u.lower(), (u, t))
    # <back> often holds availability statements as <notes> or <fn> too.
    for tag in ("notes", "fn"):
        for el in root.iter(tag):
            title_el = el.find("title")
            title = _elem_text(title_el) if title_el is not None else ""
            if _AVAIL_TITLE_RE.search(title) or _AVAIL_SECTYPE_RE.search(
                    el.get("notes-type") or el.get("fn-type") or ""):
                for u, t in extract_links(_elem_text(el), own_guids):
                    avail_urls.setdefault(u.lower(), (u, t))

    ref_urls = {}
    for reflist in root.iter("ref-list"):
        for u, t in extract_links(_elem_text(reflist), own_guids):
            ref_urls.setdefault(u.lower(), (u, t))

    all_urls = {}
    for u, t in extract_links(_elem_text(root), own_guids):
        all_urls.setdefault(u.lower(), (u, t))

    out = []
    for key, (u, t) in all_urls.items():
        if key in avail_urls:
            section = "data_availability"
        elif key in ref_urls:
            section = "references"
        else:
            section = "body"
        out.append((u, t, section))
    return out


def osf_guid_from_preprint_doi(doi):
    """'10.31234/osf.io/abc12_v3' -> 'abc12' ('' if not an OSF preprint DOI)."""
    doi = (doi or "").lower()
    if not doi.startswith(OSF_PREPRINT_PREFIXES):
        return ""
    m = re.search(r"osf\.io/([a-z0-9]+?)(?:[._]v\d+)?$", doi)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": f"jspsych-published-papers-materials/1.0 (mailto:{MAILTO})"})

MAX_RETRIES = 4
MAX_WAIT_SECONDS = 120


class RateLimited(Exception):
    """Raised when a service keeps throttling beyond our patience."""


def http_get(url, params=None, headers=None, max_bytes=None):
    """GET with retry/backoff on 429/5xx. Returns the Response, or None on
    404. Raises RateLimited when throttled past MAX_WAIT_SECONDS."""
    backoff = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, params=params, headers=headers, timeout=60,
                               stream=max_bytes is not None)
        except requests.RequestException:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403, 410):
            # Treat like not-found for our purposes (private/removed), except
            # GitHub-style rate limiting which sends 403 with a reset header.
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                raise RateLimited(url)
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise RateLimited(url) if resp.status_code == 429 \
                    else resp.raise_for_status()
            wait = backoff
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    ra_s = float(ra)
                    if ra_s > MAX_WAIT_SECONDS:
                        raise RateLimited(url)
                    wait = max(wait, ra_s)
                except ValueError:
                    pass
            time.sleep(min(wait, MAX_WAIT_SECONDS))
            backoff *= 2
            continue
        resp.raise_for_status()
        if max_bytes is not None:
            content = b""
            for chunk in resp.iter_content(65536):
                content += chunk
                if len(content) > max_bytes:
                    break
            resp._content = content
        return resp
    return None


def http_get_json(url, params=None, headers=None):
    resp = http_get(url, params=params, headers=headers)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def load_cache(path=CACHE_JSON):
    cache = {"extracted": {}, "osf_preprint": {}, "pmcid": {}, "validated": {}}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for k in cache:
                if isinstance(data.get(k), dict):
                    cache[k] = data[k]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  WARNING: could not read {path}: {exc}", file=sys.stderr)
    return cache


def save_cache(cache, path=CACHE_JSON):
    payload = {k: dict(sorted(cache[k].items())) for k in
               ("extracted", "osf_preprint", "pmcid", "validated")}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _should_reextract(entry):
    """entry: {'date': 'YYYY-MM-DD', 'status': 'ok'|'noft'} or missing."""
    if not entry:
        return True
    if entry.get("status") != "noft":
        return False
    try:
        age = (datetime.strptime(TODAY, "%Y-%m-%d")
               - datetime.strptime(entry.get("date", ""), "%Y-%m-%d")).days
    except ValueError:
        return True
    return age > NOFT_RECHECK_DAYS


# --------------------------------------------------------------------------- #
# Stage 1: Europe PMC full-text extraction
# --------------------------------------------------------------------------- #

def resolve_pmcid(paper, cache):
    """Find the PMCID (or PPR full-text id) for a paper via EPMC search.
    Returns e.g. 'PMC1234567' or 'PPR/PPR12345' or ''. Cached by paper id."""
    key = paper["id"]
    if key in cache["pmcid"]:
        return cache["pmcid"][key]
    doi = paper.get("doi") or ""
    if doi:
        query = f'DOI:"{doi}"'
    else:
        title = (paper.get("title") or "")[:100].replace('"', "")
        if not title:
            cache["pmcid"][key] = ""
            return ""
        query = f'TITLE:"{title}"'
    data = http_get_json(EPMC_SEARCH, params={
        "query": query, "format": "json", "pageSize": 3})
    result = ""
    for rec in ((data or {}).get("resultList") or {}).get("result") or []:
        if rec.get("pmcid"):
            result = rec["pmcid"]
            break
        if rec.get("source") == "PPR" and rec.get("inEPMC") == "Y":
            result = f"PPR/{rec.get('id')}"
            break
    cache["pmcid"][key] = result
    return result


def fetch_fulltext_xml(pmcid):
    """Fetch JATS XML for a PMCID (or 'PPR/PPRxxxx'). Returns text or ''."""
    if "/" in pmcid:  # PPR/PPRxxxx form
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    else:
        url = EPMC_FULLTEXT.format(pmcid=pmcid)
    resp = http_get(url)
    if resp is None or not resp.text.strip():
        return ""
    return resp.text


def stage_epmc(papers, cache, materials, own_guids, limit=None):
    """Mine EPMC full text for eligible papers not yet extracted."""
    eligible = [p for p in papers
                if p.get("fulltext_epmc") == "True"
                and _should_reextract(cache["extracted"].get(p["id"]))]
    if limit is not None:
        eligible = eligible[:limit]
    print(f"Stage 1 (EPMC full text): {len(eligible)} papers to process")
    n_links = 0
    for i, paper in enumerate(eligible, 1):
        pmcid = resolve_pmcid(paper, cache)
        if not pmcid:
            cache["extracted"][paper["id"]] = {"date": TODAY, "status": "noft"}
            continue
        xml_text = fetch_fulltext_xml(pmcid)
        if not xml_text:
            cache["extracted"][paper["id"]] = {"date": TODAY, "status": "noft"}
            continue
        own = set(own_guids)
        g = osf_guid_from_preprint_doi(paper.get("doi"))
        if g:
            own.add(g)
        links = extract_links_with_sections(xml_text, own_guids=own)
        for url, url_type, section in links:
            materials.add(paper, url, url_type, "epmc_fulltext", section)
            n_links += 1
        cache["extracted"][paper["id"]] = {"date": TODAY, "status": "ok"}
        if i % 25 == 0:
            print(f"  {i}/{len(eligible)} papers, {n_links} links so far")
            save_cache(cache)
            materials.write()
        time.sleep(0.4)  # be polite to EPMC
    print(f"  done: {n_links} links extracted")


# --------------------------------------------------------------------------- #
# Stage 2: OSF preprint -> supplemental project
# --------------------------------------------------------------------------- #

def osf_headers():
    token = os.environ.get("OSF_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def stage_osf_preprints(papers, cache, materials, limit=None):
    """For OSF-hosted preprints, look up the supplemental project node."""
    eligible = []
    for p in papers:
        guid = osf_guid_from_preprint_doi(p.get("doi"))
        if guid and _should_reextract(cache["osf_preprint"].get(p["id"])):
            eligible.append((p, guid))
    if limit is not None:
        eligible = eligible[:limit]
    print(f"Stage 2 (OSF preprints): {len(eligible)} preprints to process")
    n_links = 0
    for i, (paper, guid) in enumerate(eligible, 1):
        try:
            data = http_get_json(f"{OSF_API}/preprints/{guid}/",
                                 headers=osf_headers())
        except RateLimited:
            print("  OSF rate limited; stopping stage 2 (resumable)",
                  file=sys.stderr)
            break
        node = ((((data or {}).get("data") or {}).get("relationships") or {})
                .get("node") or {})
        node_id = ((node.get("data") or {}) or {}).get("id") or ""
        if node_id:
            materials.add(paper, f"https://osf.io/{node_id}", "osf",
                          "osf_preprint", "preprint_supplement")
            n_links += 1
        # "noft" when no supplemental node yet: authors sometimes attach one
        # later, so re-check those after NOFT_RECHECK_DAYS.
        cache["osf_preprint"][paper["id"]] = {
            "date": TODAY, "status": "ok" if node_id else "noft"}
        if i % 25 == 0:
            print(f"  {i}/{len(eligible)} preprints, {n_links} projects found")
            save_cache(cache)
            materials.write()
        time.sleep(0.3)
    print(f"  done: {n_links} supplemental projects found")


# --------------------------------------------------------------------------- #
# Stage 3: validation (does the repository contain jsPsych code?)
# --------------------------------------------------------------------------- #

_JSPSYCH_NAME_RE = re.compile(r"jspsych", re.I)
_JSPSYCH_CONTENT_RE = re.compile(rb"jsPsych|jspsych", re.I)
_CODE_EXT_RE = re.compile(r"\.(html?|js|mjs|cjs)$", re.I)


def _osf_resolve_guid(guid_url):
    """Resolve an osf.io GUID to its resource type ('nodes', 'registrations',
    'preprints', 'files', 'users', ...). Returns (type, id) or ('', '')."""
    guid = guid_url.rstrip("/").rsplit("/", 1)[-1]
    data = http_get_json(f"{OSF_API}/guids/{guid}/?resolve=false",
                         headers=osf_headers())
    d = (data or {}).get("data") or {}
    referent = ((d.get("relationships") or {}).get("referent") or {}) \
        .get("data") or {}
    return referent.get("type") or "", referent.get("id") or guid


def _osf_check_files(resource_type, node_id):
    """BFS the osfstorage file tree of an OSF node/registration looking for
    jsPsych markers. Returns True/False, or None if the check was cut short
    without finding anything."""
    base = f"{OSF_API}/{resource_type}/{node_id}"
    queue = [f"{base}/files/osfstorage/?page[size]=100"]
    # Include first-level child components (code often lives in one).
    children = http_get_json(f"{base}/children/?page[size]=25",
                             headers=osf_headers())
    for child in ((children or {}).get("data") or []):
        cid = child.get("id")
        if cid:
            queue.append(
                f"{OSF_API}/{resource_type}/{cid}/files/osfstorage/?page[size]=100")
    requests_used = 1 + (1 if children is not None else 0)
    downloads = 0
    candidates = []  # small html/js files to grep if no name marker found
    truncated = False

    while queue and requests_used < OSF_MAX_REQUESTS_PER_NODE:
        url = queue.pop(0)
        data = http_get_json(url, headers=osf_headers())
        requests_used += 1
        if not data:
            continue
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            name = attrs.get("name") or ""
            kind = attrs.get("kind")
            if _JSPSYCH_NAME_RE.search(name):
                return True
            if kind == "folder":
                rel = ((item.get("relationships") or {}).get("files") or {})
                link = ((rel.get("links") or {}).get("related") or {})
                href = link.get("href") if isinstance(link, dict) else link
                if href:
                    queue.append(href + ("&" if "?" in href else "?")
                                 + "page[size]=100")
            elif kind == "file" and _CODE_EXT_RE.search(name):
                size = attrs.get("size") or 0
                dl = ((item.get("links") or {}).get("download"))
                if dl and size and size <= OSF_MAX_FILE_BYTES:
                    candidates.append((size, dl))
        nxt = ((data.get("links") or {}).get("next"))
        if nxt:
            queue.append(nxt)
    if queue:
        truncated = True

    # No filename marker: grep the smallest few html/js files.
    for _, dl in sorted(candidates)[:OSF_MAX_FILE_DOWNLOADS]:
        if downloads >= OSF_MAX_FILE_DOWNLOADS:
            break
        resp = http_get(dl, headers=osf_headers(), max_bytes=OSF_MAX_FILE_BYTES)
        downloads += 1
        if resp is not None and _JSPSYCH_CONTENT_RE.search(resp.content or b""):
            return True
    if truncated or (candidates and downloads == 0):
        return None
    return False


def gh_headers():
    token = os.environ.get("GITHUB_TOKEN")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _github_check(url):
    """Check a GitHub repo for jsPsych markers via the git tree API.
    Returns True/False/None."""
    owner_repo = url.split("github.com/", 1)[1]
    repo = http_get_json(f"{GITHUB_API}/repos/{owner_repo}",
                         headers=gh_headers())
    if not repo:
        return None
    branch = repo.get("default_branch") or "main"
    tree = http_get_json(
        f"{GITHUB_API}/repos/{owner_repo}/git/trees/{branch}?recursive=1",
        headers=gh_headers())
    if not tree:
        return None
    paths = [t.get("path") or "" for t in tree.get("tree") or []]
    if any(_JSPSYCH_NAME_RE.search(p) for p in paths):
        return True
    # Grep a few small html/js files via raw.githubusercontent.com.
    code_files = [p for p in paths if _CODE_EXT_RE.search(p)]
    code_files.sort(key=lambda p: (0 if "index" in p.lower() else 1, len(p)))
    for p in code_files[:GH_MAX_FILE_DOWNLOADS]:
        resp = http_get(
            f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{p}",
            max_bytes=OSF_MAX_FILE_BYTES)
        if resp is not None and _JSPSYCH_CONTENT_RE.search(resp.content or b""):
            return True
    if tree.get("truncated") or len(code_files) > GH_MAX_FILE_DOWNLOADS:
        return None
    return False


def _zenodo_check(url):
    """Check a Zenodo record's file names for jsPsych markers.
    Returns True/False/None (None when files are hidden or all archives)."""
    rec_id = url.rstrip("/").rsplit("/", 1)[-1]
    data = http_get_json(f"{ZENODO_API}/{rec_id}")
    if not data:
        return None
    files = data.get("files") or []
    if not files:
        return None
    names = [f.get("key") or "" for f in files]
    if any(_JSPSYCH_NAME_RE.search(n) for n in names):
        return True
    # Grep small direct html/js files (zips are opaque; leave those unknown).
    for f in files:
        name = f.get("key") or ""
        size = f.get("size") or 0
        link = ((f.get("links") or {}).get("self"))
        if _CODE_EXT_RE.search(name) and link and size <= OSF_MAX_FILE_BYTES:
            resp = http_get(link, max_bytes=OSF_MAX_FILE_BYTES)
            if resp is not None and _JSPSYCH_CONTENT_RE.search(
                    resp.content or b""):
                return True
    if any(n.lower().endswith((".zip", ".tar.gz", ".tgz", ".7z", ".rar"))
           for n in names):
        return None
    return False


def stage_validate(cache, materials, limit=None):
    """Validate every distinct un-validated URL in materials."""
    urls = materials.distinct_urls()
    todo = [(u, t) for u, t in urls if u.lower() not in cache["validated"]]
    if limit is not None:
        todo = todo[:limit]
    print(f"Stage 3 (validation): {len(todo)} of {len(urls)} URLs to check")
    github_ok = True
    osf_ok = True
    n_done = 0
    for i, (url, url_type) in enumerate(todo, 1):
        verdict = None
        checked = False
        try:
            if url_type == "osf" and osf_ok:
                rtype, rid = _osf_resolve_guid(url)
                if rtype in ("nodes", "registrations"):
                    verdict = _osf_check_files(rtype, rid)
                elif rtype:  # preprints/users/files — not a materials project
                    verdict = False
                checked = True
                time.sleep(0.3)
            elif url_type == "github" and github_ok:
                verdict = _github_check(url)
                checked = True
                time.sleep(0.5)
            elif url_type == "zenodo":
                verdict = _zenodo_check(url)
                checked = True
                time.sleep(0.5)
            # gitlab/pavlovia/gorilla: left unvalidated for now.
        except RateLimited:
            if url_type == "osf":
                osf_ok = False
                print("  OSF rate limited; skipping remaining OSF URLs "
                      "(set OSF_TOKEN and re-run)", file=sys.stderr)
            elif url_type == "github":
                github_ok = False
                print("  GitHub rate limited; skipping remaining GitHub URLs",
                      file=sys.stderr)
            continue
        if checked:
            cache["validated"][url.lower()] = {
                "confirmed": verdict, "date": TODAY}
            n_done += 1
        if i % 20 == 0:
            print(f"  {i}/{len(todo)} URLs checked")
            save_cache(cache)
    print(f"  done: {n_done} URLs validated this run")


# --------------------------------------------------------------------------- #
# Materials store
# --------------------------------------------------------------------------- #

class MaterialsStore:
    """In-memory materials table with upsert semantics, persisted as CSV."""

    def __init__(self, path=MATERIALS_CSV):
        self.path = path
        self.rows = {}  # (paper_id, url.lower()) -> row dict
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    key = (row.get("paper_id"), (row.get("url") or "").lower())
                    self.rows[key] = {c: row.get(c, "") for c in MATERIALS_COLUMNS}

    def add(self, paper, url, url_type, source, section):
        key = (paper["id"], url.lower())
        existing = self.rows.get(key)
        if existing:
            # Keep the row but refresh section/source if the new signal is
            # stronger (data_availability beats body beats references).
            rank = {"preprint_supplement": 0, "data_availability": 1,
                    "body": 2, "unknown": 3, "references": 4}
            if rank.get(section, 5) < rank.get(existing.get("section"), 5):
                existing["section"] = section
                existing["source"] = source
            return
        self.rows[key] = {
            "paper_id": paper["id"],
            "doi": paper.get("doi") or "",
            "url": url,
            "url_type": url_type,
            "source": source,
            "section": section,
            "jspsych_confirmed": "",
            "checked_date": TODAY,
        }

    def distinct_urls(self):
        seen = {}
        for row in self.rows.values():
            seen.setdefault(row["url"].lower(), (row["url"], row["url_type"]))
        return list(seen.values())

    def apply_validation(self, validated):
        for row in self.rows.values():
            v = validated.get(row["url"].lower())
            if v is not None:
                c = v.get("confirmed")
                row["jspsych_confirmed"] = "" if c is None else str(bool(c))

    def write(self):
        rows = sorted(self.rows.values(),
                      key=lambda r: (r["paper_id"], r["url"].lower()))
        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MATERIALS_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def load_papers():
    with open(PAPERS_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if r.get("exclude") != "True"]


def build_own_guids(papers):
    """OSF GUIDs that are preprint DOIs in the dataset: links to these are
    citations of papers, not materials repositories."""
    guids = set()
    for p in papers:
        g = osf_guid_from_preprint_doi(p.get("doi"))
        if g:
            guids.add(g)
    return guids


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="max papers per extraction stage this run")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip the validation stage")
    ap.add_argument("--validate-only", action="store_true",
                    help="skip extraction; only validate pending URLs")
    ap.add_argument("--validate-limit", type=int, default=None,
                    help="max URLs to validate this run")
    args = ap.parse_args(argv)

    papers = load_papers()
    cache = load_cache()
    materials = MaterialsStore()
    own_guids = build_own_guids(papers)
    print(f"{len(papers)} papers loaded ({len(materials.rows)} existing "
          f"materials rows)")

    if not args.validate_only:
        stage_epmc(papers, cache, materials, own_guids, limit=args.limit)
        save_cache(cache)
        materials.write()
        stage_osf_preprints(papers, cache, materials, limit=args.limit)
        save_cache(cache)
        materials.write()

    if not args.no_validate:
        stage_validate(cache, materials, limit=args.validate_limit)
        save_cache(cache)

    materials.apply_validation(cache["validated"])
    materials.write()

    n = len(materials.rows)
    confirmed = sum(1 for r in materials.rows.values()
                    if r["jspsych_confirmed"] == "True")
    papers_with = len({r["paper_id"] for r in materials.rows.values()})
    papers_confirmed = len({r["paper_id"] for r in materials.rows.values()
                            if r["jspsych_confirmed"] == "True"})
    summary = (f"materials.csv: {n} links across {papers_with} papers; "
               f"{confirmed} links jsPsych-confirmed "
               f"({papers_confirmed} papers)")
    print("\n" + summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")


if __name__ == "__main__":
    main()
