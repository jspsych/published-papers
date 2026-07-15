#!/usr/bin/env python3
"""Generate author and institution summaries from the paper dataset.

Reads data/papers.csv and data/authors.csv and writes:

  analysis/authors.csv       one row per unique author
  analysis/institutions.csv  one row per unique institution

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
import os
import re
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

PAPERS_CSV = os.path.join(REPO, "data", "papers.csv")
AUTHORS_IN_CSV = os.path.join(REPO, "data", "authors.csv")

AUTHORS_OUT_CSV = os.path.join(HERE, "authors.csv")
INSTITUTIONS_OUT_CSV = os.path.join(HERE, "institutions.csv")

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


def norm_name(name):
    """Normalize a name for matching: lowercase, strip periods, collapse ws."""
    name = name.lower().replace(".", " ")
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
    for row in raw_papers:
        pid = row["id"]
        if (row.get("exclude") or "").strip() == "True":
            excluded.add(pid)
            continue
        if (row.get("type") or "").strip().lower() in NON_RESEARCH_TYPES:
            excluded.add(pid)
            type_filtered += 1
            continue
        group_dates.setdefault(group_of[pid], []).append(paper_date(row))

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

    # Remap author keys inside institution author sets so n_authors doesn't
    # double-count people whose name-only rows were merged above.
    for ientry in institutions.values():
        ientry["authors"] = {author_key_remap.get(k, k)
                             for k in ientry["authors"]}

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
    print("  Top 5 authors by n_papers:")
    for r in author_rows[:5]:
        print(f"    {r['n_papers']:>4}  {r['author_name']}  ({r['author_key']})")
    print("  Top 5 institutions by n_papers:")
    for r in inst_rows[:5]:
        print(f"    {r['n_papers']:>4}  {r['institution_name']} "
              f"[{r['country']}]  ({r['institution_key']})")


if __name__ == "__main__":
    main()
