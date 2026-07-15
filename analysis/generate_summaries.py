#!/usr/bin/env python3
"""Generate author and institution summaries from the paper dataset.

Reads data/papers.csv and data/authors.csv and writes:

  analysis/authors.csv       one row per unique author
  analysis/institutions.csv  one row per unique institution
  analysis/journals.csv      one row per unique venue (normalized name)
  analysis/dashboard.json    compact summary consumed by the Pages dashboard

Papers whose `exclude` column is the string "True" in data/papers.csv are
excluded from both summaries, as are papers whose `type` is one of the
non-research types in NON_RESEARCH_TYPES (the raw data keeps everything).

Deduplication caveats
---------------------
Authors are keyed by ORCID when present, otherwise by a normalized form of
the name (lowercase, whitespace collapsed, periods stripped). Because ORCID
coverage is inconsistent in the sources, a second pass merges a name-only
group into an ORCID group when its normalized name matches EXACTLY ONE
ORCID key seen elsewhere in the data; if the same name is seen with two or
more different ORCIDs the name-only group is left separate (ambiguous).
Even so, name-only matching cannot distinguish two different people who
share an identical name, nor unify variant spellings of the same person
("J. R. de Leeuw" vs "Joshua R. de Leeuw") unless records carry the same
ORCID. Counts for authors without ORCIDs are therefore approximate.
Institutions are keyed by ROR id when present, else normalized name, with
the same unambiguous-match merge and the same limitation.

Stdlib only — no third-party dependencies.
"""

import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

PAPERS_CSV = os.path.join(REPO, "data", "papers.csv")
AUTHORS_IN_CSV = os.path.join(REPO, "data", "authors.csv")

AUTHORS_OUT_CSV = os.path.join(HERE, "authors.csv")
INSTITUTIONS_OUT_CSV = os.path.join(HERE, "institutions.csv")
JOURNALS_OUT_CSV = os.path.join(HERE, "journals.csv")
DASHBOARD_OUT_JSON = os.path.join(HERE, "dashboard.json")

# Work types excluded from the analysis (the raw data CSVs keep everything).
# A paper is skipped only when its WHOLE lowercased `type` string equals one
# of these values — compound EPMC type strings like
# "research-article; Journal Article" must NOT match. These types are not
# research papers using jsPsych: software releases, peer-review records,
# paratext (covers/edboards), errata, and datasets.
NON_RESEARCH_TYPES = {"software", "peer-review", "paratext", "erratum", "dataset"}

AUTHOR_OUT_COLUMNS = [
    "author_key", "author_name", "orcid", "n_papers", "first_use", "last_use",
]
INSTITUTION_OUT_COLUMNS = [
    "institution_key", "institution_name", "ror", "country",
    "n_papers", "n_authors", "first_use", "last_use",
]
JOURNAL_OUT_COLUMNS = [
    "journal_key", "journal_name", "field", "n_papers", "n_authors",
    "first_use", "last_use",
]

# Venue fallback for works whose canonical member has a blank venue: infer
# the venue from the DOI registrant prefix. OpenAlex leaves the venue blank
# on most PsyArXiv/OSF preprint records, so without this the biggest preprint
# servers vanish from the journals table. Used ONLY in the journals
# aggregation — the raw data is never modified. 10.1101 is shared by bioRxiv
# and medRxiv and cannot be distinguished by prefix alone; it is labeled
# jointly (and only applies when the venue is blank, which is rare there).
DOI_PREFIX_VENUES = {
    "10.31234": "PsyArXiv",
    "10.31219": "OSF Preprints",
    "10.31235": "SocArXiv",
    "10.35542": "EdArXiv",
    "10.48550": "arXiv",
    "10.21203": "Research Square",
    "10.20944": "Preprints.org",
    "10.2139": "SSRN",
    "10.1101": "bioRxiv/medRxiv",
}

# Alternate venue spellings (normalized key -> display name) merged into the
# DOI_PREFIX_VENUES fallback rows above, so a server whose works arrive both
# with an OpenAlex venue string and via the blank-venue fallback doesn't
# split into two rows. Only ACTUAL splits observed in the data are listed —
# venues are not renamed for their own sake. (bioRxiv (Cold Spring Harbor
# Laboratory), medRxiv, and arXiv (Cornell University) are deliberately NOT
# aliased: the "bioRxiv/medRxiv" fallback row is ambiguous between two
# servers, and arXiv has no fallback row to merge with.)
VENUE_ALIASES = {
    "psyarxiv osf preprints": "PsyArXiv",
    "osf preprints osf preprints": "OSF Preprints",
}


def modal_value(counter):
    """Most frequent value in a Counter; ties broken by count then
    alphabetically (deterministic). '' for an empty counter."""
    if not counter:
        return ""
    return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def norm_name(name):
    """Normalize a name for matching: lowercase, strip periods, collapse ws."""
    name = name.lower().replace(".", " ")
    return re.sub(r"\s+", " ", name).strip()


def norm_venue(name):
    """Normalize a venue name for keying: lowercase, collapse runs of
    punctuation and whitespace to single spaces, trim. This merges
    MEDLINE-style variants ('Journal of experimental psychology. General'
    vs 'Journal of Experimental Psychology General') without merging venues
    whose words actually differ. We have no ISSNs to key on."""
    name = re.sub(r"[\W_]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", name).strip()


def split_list(value):
    """Split a semicolon-joined list column into a list ('' -> [])."""
    value = (value or "").strip()
    if not value:
        return []
    return [part.strip() for part in value.split(";")]


def paper_date(paper):
    """Best available date string for a paper: full date, else YYYY, else ''."""
    date = (paper.get("publication_date") or "").strip()
    if date:
        return date
    year = (paper.get("publication_year") or "").strip()
    return year  # may be ''


def main():
    # ------------------------------------------------------------------ #
    # Load papers. Preprints linked to their published version (via the
    # duplicate_of column in data/papers.csv) are collapsed into one "work":
    # every row is mapped to its canonical group id, and each group counts
    # once. A group's date range spans its NON-excluded members (so a
    # preprint date correctly marks first use); excluded members are dropped,
    # and a group whose members are ALL excluded disappears entirely.
    # ------------------------------------------------------------------ #
    raw_papers = []
    with open(PAPERS_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw_papers.append(row)

    # Resolve duplicate_of transitively to a canonical group id. The update
    # script already flattens chains, but resolve defensively (and guard
    # against cycles) so this script is robust on its own.
    dup = {r["id"]: (r.get("duplicate_of") or "").strip() for r in raw_papers}

    def canonical(pid):
        seen = set()
        cur = pid
        while dup.get(cur) and cur not in seen:
            seen.add(cur)
            cur = dup[cur]
        return cur

    group_of = {r["id"]: canonical(r["id"]) for r in raw_papers}
    group_size = Counter(group_of.values())  # total members per group

    # Per-member exclusion; non-excluded members contribute a date to their
    # group. `excluded` holds paper ids skipped during author aggregation.
    excluded = set()
    type_filtered = 0
    group_dates = {}  # group id -> list of member date strings (non-excluded)
    group_field_votes = {}  # group id -> Counter of non-blank topic_field
    for row in raw_papers:
        pid = row["id"]
        if (row.get("exclude") or "").strip() == "True":
            excluded.add(pid)
            continue
        if (row.get("type") or "").strip().lower() in NON_RESEARCH_TYPES:
            excluded.add(pid)
            type_filtered += 1
            continue
        gid = group_of[pid]
        group_dates.setdefault(gid, []).append(paper_date(row))
        tfield = (row.get("topic_field") or "").strip()
        if tfield:
            group_field_votes.setdefault(gid, Counter())[tfield] += 1

    # A work's field: modal topic_field across its non-excluded members
    # (member versions of the same work almost always agree; using the modal
    # value lets a classified preprint cover an unclassified published row
    # and vice versa). '' when no member is classified.
    work_field = {gid: modal_value(group_field_votes[gid])
                  for gid in group_field_votes}

    # first/last use per active group (min/max across its non-excluded members)
    group_span = {}
    for gid, ds in group_dates.items():
        ordered = sorted(d for d in ds if d)
        group_span[gid] = (ordered[0], ordered[-1]) if ordered else ("", "")

    # `dates` maps a paper id to its group id, but ONLY for non-excluded
    # members of active groups — used below to accept/skip author rows and to
    # collapse an author's papers onto distinct works.
    dates = {}
    for row in raw_papers:
        pid = row["id"]
        if pid in excluded:
            continue
        dates[pid] = group_of[pid]

    multi_member_groups = sum(1 for gid in group_dates if group_size[gid] > 1)

    # ------------------------------------------------------------------ #
    # Journals: one entry per unique venue, keyed by normalized name (no
    # ISSNs in the data). A work's venue is its CANONICAL member's venue,
    # so a preprint linked to its published article counts for the journal,
    # not the preprint server; preprint-only works count under their server.
    # ------------------------------------------------------------------ #
    by_id = {r["id"]: r for r in raw_papers}
    # journal_key -> {"names": Counter, "papers": set(gid), "authors": set}
    journals = {}
    group_journal = {}  # gid -> journal_key (only for works with a venue)
    skipped_no_venue = 0
    venue_from_doi_prefix = 0
    for gid in group_dates:  # active works only (>=1 non-excluded member)
        crow = by_id.get(gid)
        if crow is None:
            # Dangling duplicate_of target — should not happen (rows are
            # never deleted), but stay consistent with canonical()'s
            # defensive posture rather than KeyError-ing.
            print(f"  WARNING: canonical id {gid} not found in papers.csv; "
                  f"skipping its journal attribution")
            skipped_no_venue += 1
            continue
        venue = (crow.get("venue") or "").strip()
        if not venue:
            # Blank venue: infer the preprint server from the canonical
            # member's DOI registrant prefix (journals-table only).
            doi = (crow.get("doi") or "").strip()
            prefix = doi.split("/", 1)[0] if doi else ""
            venue = DOI_PREFIX_VENUES.get(prefix, "")
            if venue:
                venue_from_doi_prefix += 1
        nvenue = norm_venue(venue)
        if not nvenue:
            skipped_no_venue += 1
            continue
        alias_display = VENUE_ALIASES.get(nvenue)
        if alias_display:
            venue = alias_display  # merged rows display the server name
            nvenue = norm_venue(venue)
        j_key = f"name:{nvenue}"
        jentry = journals.setdefault(
            j_key, {"names": Counter(), "papers": set(), "authors": set(),
                    "fields": Counter()})
        jentry["names"][venue] += 1  # one spelling vote per work
        jentry["papers"].add(gid)
        wf = work_field.get(gid, "")
        if wf:
            jentry["fields"][wf] += 1  # one field vote per classified work
        group_journal[gid] = j_key

    # ------------------------------------------------------------------ #
    # Aggregate authors and institutions
    # ------------------------------------------------------------------ #
    # author_key -> {"names": Counter, "orcid": str, "papers": set}
    authors = {}
    # inst_key -> {"names": Counter, "ror": str, "countries": Counter,
    #              "papers": set, "authors": set}
    institutions = {}

    # For the second-pass unambiguous merges: normalized name -> set of
    # orcid:/ror: keys that name has been seen with.
    name_to_orcid_keys = {}
    name_to_ror_keys = {}

    skipped_unnamed = 0
    skipped_empty_norm = 0
    skipped_email_insts = 0
    length_mismatch_rows = 0

    with open(AUTHORS_IN_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pid = row["paper_id"]
            if pid in excluded or pid not in dates:
                continue
            # Collapse this member onto its canonical work: authors and
            # institutions are counted once per WORK, not once per member row.
            gid = dates[pid]

            name = (row.get("author_name") or "").strip()
            orcid = (row.get("orcid") or "").strip()
            nname = norm_name(name)

            if orcid:
                a_key = f"orcid:{orcid}"
                if nname:
                    name_to_orcid_keys.setdefault(nname, set()).add(a_key)
            elif nname:
                a_key = f"name:{nname}"
            elif name:
                # Name normalizes to "" (e.g. just periods): keying it would
                # merge unrelated entities under a shared empty bucket.
                print(f"  WARNING: skipping author with degenerate name "
                      f"{name!r} on paper {pid}")
                skipped_empty_norm += 1
                continue
            else:
                skipped_unnamed += 1
                continue

            entry = authors.setdefault(
                a_key, {"names": Counter(), "orcid": orcid, "papers": set()})
            if name:
                entry["names"][name] += 1
            entry["papers"].add(gid)

            # Journal attribution: union of author keys across the work's
            # members (same semantics as institutions' n_authors).
            j_key = group_journal.get(gid)
            if j_key:
                journals[j_key]["authors"].add(a_key)

            # -------------------------------------------------------- #
            # Institutions: positionally parallel semicolon-joined
            # lists. On a length mismatch we cannot trust alignment, so
            # log the row and keep the names WITHOUT any country/ROR
            # attribution rather than risk attaching them wrongly.
            # -------------------------------------------------------- #
            names = split_list(row.get("institutions"))
            countries = split_list(row.get("institution_countries"))
            rors = split_list(row.get("institution_rors"))
            nonzero_lengths = {len(x) for x in (names, countries, rors) if x}
            if len(nonzero_lengths) > 1:
                length_mismatch_rows += 1
                print(f"  WARNING: institution list length mismatch on paper "
                      f"{pid} (author {name!r}); ignoring countries/RORs for "
                      f"this row")
                countries = [""] * len(names)
                rors = [""] * len(names)
            else:
                n = max(len(names), len(countries), len(rors))
                names += [""] * (n - len(names))
                countries += [""] * (n - len(countries))
                rors += [""] * (n - len(rors))

            for inst_name, country, ror in zip(names, countries, rors):
                if "@" in inst_name:
                    # Fragment of an EPMC free-text affiliation containing a
                    # semicolon-separated email address; not an institution.
                    skipped_email_insts += 1
                    continue
                norm_inst = norm_name(inst_name)
                if ror:
                    i_key = f"ror:{ror}"
                    if norm_inst:
                        name_to_ror_keys.setdefault(norm_inst, set()).add(i_key)
                elif norm_inst:
                    i_key = f"name:{norm_inst}"
                elif inst_name:
                    print(f"  WARNING: skipping institution with degenerate "
                          f"name {inst_name!r} on paper {pid}")
                    skipped_empty_norm += 1
                    continue
                else:
                    continue
                ientry = institutions.setdefault(
                    i_key, {"names": Counter(), "ror": ror,
                            "countries": Counter(), "papers": set(),
                            "authors": set()})
                if inst_name:
                    ientry["names"][inst_name] += 1
                if country:
                    ientry["countries"][country] += 1
                ientry["papers"].add(gid)
                ientry["authors"].add(a_key)

    # ------------------------------------------------------------------ #
    # Second pass: merge name-only groups into ORCID/ROR groups when the
    # normalized name unambiguously maps to exactly one identifier key.
    # (ORCID/ROR coverage is inconsistent in the sources, so the same
    # person/institution otherwise splits into two rows.)
    # ------------------------------------------------------------------ #
    author_key_remap = {}  # merged name: key -> orcid: key
    for name_key in [k for k in authors if k.startswith("name:")]:
        targets = name_to_orcid_keys.get(name_key[len("name:"):])
        if targets and len(targets) == 1:
            target_key = next(iter(targets))
            target = authors[target_key]
            source = authors.pop(name_key)
            target["names"] += source["names"]
            target["papers"] |= source["papers"]
            author_key_remap[name_key] = target_key
    merged_authors = len(author_key_remap)

    merged_institutions = 0
    for name_key in [k for k in institutions if k.startswith("name:")]:
        targets = name_to_ror_keys.get(name_key[len("name:"):])
        if targets and len(targets) == 1:
            target_key = next(iter(targets))
            target = institutions[target_key]
            source = institutions.pop(name_key)
            target["names"] += source["names"]
            target["countries"] += source["countries"]
            target["papers"] |= source["papers"]
            target["authors"] |= source["authors"]
            merged_institutions += 1

    # Remap author keys inside institution and journal author sets so
    # n_authors doesn't double-count people whose name-only rows were
    # merged above.
    for ientry in institutions.values():
        ientry["authors"] = {author_key_remap.get(k, k)
                             for k in ientry["authors"]}
    for jentry in journals.values():
        jentry["authors"] = {author_key_remap.get(k, k)
                             for k in jentry["authors"]}

    # ------------------------------------------------------------------ #
    # Helpers for output rows
    # ------------------------------------------------------------------ #
    def most_frequent(counter):
        """Most frequent value; ties broken alphabetically (deterministic)."""
        if not counter:
            return ""
        return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    def date_range(group_ids):
        """Min first_use / max last_use across a set of canonical works."""
        firsts = [group_span[g][0] for g in group_ids if group_span.get(g, ("", ""))[0]]
        lasts = [group_span[g][1] for g in group_ids if group_span.get(g, ("", ""))[1]]
        if not firsts:
            return "", ""
        return min(firsts), max(lasts)

    # ------------------------------------------------------------------ #
    # Write authors.csv
    # ------------------------------------------------------------------ #
    author_rows = []
    for key, entry in authors.items():
        first, last = date_range(entry["papers"])
        author_rows.append({
            "author_key": key,
            "author_name": most_frequent(entry["names"]),
            "orcid": entry["orcid"],
            "n_papers": len(entry["papers"]),
            "first_use": first,
            "last_use": last,
        })
    author_rows.sort(key=lambda r: (-r["n_papers"], r["author_key"]))

    with open(AUTHORS_OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=AUTHOR_OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(author_rows)

    # ------------------------------------------------------------------ #
    # Write institutions.csv
    # ------------------------------------------------------------------ #
    inst_rows = []
    for key, entry in institutions.items():
        first, last = date_range(entry["papers"])
        inst_rows.append({
            "institution_key": key,
            "institution_name": most_frequent(entry["names"]),
            "ror": entry["ror"],
            "country": most_frequent(entry["countries"]),
            "n_papers": len(entry["papers"]),
            "n_authors": len(entry["authors"]),
            "first_use": first,
            "last_use": last,
        })
    inst_rows.sort(key=lambda r: (-r["n_papers"], r["institution_key"]))

    with open(INSTITUTIONS_OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=INSTITUTION_OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(inst_rows)

    # ------------------------------------------------------------------ #
    # Write journals.csv
    # ------------------------------------------------------------------ #
    journal_rows = []
    for key, entry in journals.items():
        first, last = date_range(entry["papers"])
        journal_rows.append({
            "journal_key": key,
            "journal_name": most_frequent(entry["names"]),
            # Modal topic_field across the journal's classified works (ties:
            # count then alphabetical); '' when no member work is classified.
            "field": modal_value(entry["fields"]),
            "n_papers": len(entry["papers"]),
            "n_authors": len(entry["authors"]),
            "first_use": first,
            "last_use": last,
        })
    journal_rows.sort(key=lambda r: (-r["n_papers"], r["journal_key"]))

    with open(JOURNALS_OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=JOURNAL_OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(journal_rows)

    # ------------------------------------------------------------------ #
    # Write dashboard.json (everything the GitHub Pages dashboard needs
    # in one small file). All ordering is deterministic: works_by_year is
    # sorted by year, and the top-N lists inherit the deterministic sort
    # of the tables above.
    # ------------------------------------------------------------------ #
    preprints_linked = sum(
        1 for r in raw_papers if (r.get("duplicate_of") or "").strip())

    year_counts = Counter()
    for gid in group_dates:
        first = group_span.get(gid, ("", ""))[0]
        if len(first) >= 4 and first[:4].isdigit():
            year_counts[first[:4]] += 1
    works_by_year = [{"year": int(y), "n": year_counts[y]}
                     for y in sorted(year_counts)]

    def top_entries(rows, name_field, limit=25, extra_keys=()):
        out = []
        for r in rows[:limit]:
            e = {
                "name": r[name_field],
                "n_papers": r["n_papers"],
                "first_use": r["first_use"],
                "last_use": r["last_use"],
            }
            for k in extra_keys:
                e[k] = r[k]
            out.append(e)
        return out

    dashboard = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "totals": {
            "works": len(group_dates),
            "papers_rows": len(raw_papers),
            "authors": len(author_rows),
            "institutions": len(inst_rows),
            "journals": len(journal_rows),
            "preprints_linked": preprints_linked,
        },
        "works_by_year": works_by_year,
        "top_journals": top_entries(journal_rows, "journal_name",
                                    extra_keys=("field",)),
        "top_institutions": top_entries(inst_rows, "institution_name"),
        "top_authors": top_entries(author_rows, "author_name"),
    }
    with open(DASHBOARD_OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(dashboard, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    print("Analysis summary")
    print(f"  Papers excluded (exclude == True): {len(excluded) - type_filtered}")
    print(f"  Papers filtered by non-research type: {type_filtered}")
    print(f"  Papers analyzed (non-excluded members): {len(dates)}")
    print(f"  Distinct works (after linking): {len(group_dates)}")
    print(f"  Multi-member works (>1 linked member): {multi_member_groups}")
    print(f"  Author rows skipped (no name, no ORCID): {skipped_unnamed}")
    if skipped_empty_norm:
        print(f"  Entries skipped (degenerate name): {skipped_empty_norm}")
    if skipped_email_insts:
        print(f"  Institution entries dropped (email fragments): {skipped_email_insts}")
    if length_mismatch_rows:
        print(f"  Rows with institution list length mismatch: {length_mismatch_rows}")
    print(f"  Name-only author groups merged into ORCID groups: {merged_authors}")
    print(f"  Name-only institution groups merged into ROR groups: {merged_institutions}")
    print(f"  Unique authors: {len(author_rows)}")
    print(f"  Unique institutions: {len(inst_rows)}")
    print(f"  Unique journals: {len(journal_rows)} "
          f"(venues recovered from DOI prefix: {venue_from_doi_prefix}, "
          f"works skipped for empty venue: {skipped_no_venue})")

    # Field-classification coverage
    n_works = len(group_dates)
    journal_field = {r["journal_key"]: r["field"] for r in journal_rows}
    direct = sum(1 for gid in group_dates if work_field.get(gid, ""))
    covered = sum(
        1 for gid in group_dates
        if work_field.get(gid, "")
        or journal_field.get(group_journal.get(gid, ""), ""))
    classified_journals = sum(1 for r in journal_rows if r["field"])
    def pct(a, b):
        return f"{100.0 * a / b:.1f}%" if b else "n/a"
    print(f"  Field coverage: {pct(direct, n_works)} of works classified "
          f"directly ({direct}/{n_works}); "
          f"{pct(classified_journals, len(journal_rows))} of journals "
          f"classified ({classified_journals}/{len(journal_rows)}); "
          f"{pct(covered, n_works)} of works in a classified journal or "
          f"classified directly ({covered}/{n_works})")
    print("  Top 5 authors by n_papers:")
    for r in author_rows[:5]:
        print(f"    {r['n_papers']:>4}  {r['author_name']}  ({r['author_key']})")
    print("  Top 5 institutions by n_papers:")
    for r in inst_rows[:5]:
        print(f"    {r['n_papers']:>4}  {r['institution_name']} "
              f"[{r['country']}]  ({r['institution_key']})")
    print("  Top 5 journals by n_papers:")
    for r in journal_rows[:5]:
        print(f"    {r['n_papers']:>4}  {r['journal_name']}  "
              f"({r['journal_key']})")


if __name__ == "__main__":
    main()
