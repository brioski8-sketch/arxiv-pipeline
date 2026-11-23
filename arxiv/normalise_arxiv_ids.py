#!/usr/bin/env python3
"""Normalise papers.arxiv_id to ONE canonical format.

Why this exists
---------------
`arxiv_id` is a UNIQUE key, but it was being written in two shapes for the same
paper: `update_arxiv.py` stored the raw Atom <id> (a full URL carrying a version,
`http://arxiv.org/abs/2403.12108v3`) while `update_influential.py` stored a bare id
(`2403.12108`). Two consequences:

  1. URL-form and bare-form rows for the same paper coexist, defeating UNIQUE.
  2. Because the VERSION is part of the key, a later arXiv version (v1 -> v2)
     inserts a NEW row instead of updating the existing one. Measured 2026-10-05:
     5,194 rows / 5,145 distinct papers / 45 duplicate groups / 49 surplus rows.

Canonical form
--------------
Bare arXiv id, no URL, no version suffix — `2403.12108`. The version is preserved
in a new `version` column and the full URL stays in `links`, so nothing is lost.
Old-style ids (`hep-th/0601001`) keep their slash form; only a URL prefix and a
trailing `vN` are ever removed.

Merge rules (survivor = lowest id, so the earliest row's identity survives)
--------------------------------------------------------------------------
  arxiv_id            canonical bare id
  version             MAX seen version
  title/updated/summary/authors/links   from the highest-version row (newest text)
  published           MIN  (original submission date)
  ingested_at         MIN  (a version bump must NOT refresh the briefing window)
  citation_count / influential_citation_count   MAX
  relevance_score     MAX, with that row's relevance_categories
  deep_dived          MAX (1 if any row was dived)
  category list       a value that looks like real categories ('cs.CY; cs.AI')
                      rather than the string 'influential' the channel writes there
  source              'influential' if ANY row was influential, else 'fresh'

Usage
-----
    python3 normalise_arxiv_ids.py --dry-run     # report only, write nothing
    python3 normalise_arxiv_ids.py               # back up, then migrate
"""
import argparse
import datetime
import os
import re
import shutil
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "arxiv_papers.db")

URL_PREFIX = re.compile(r"^https?://arxiv\.org/abs/")
VERSION_SUFFIX = re.compile(r"v(\d+)$")


def canon(aid: str) -> str:
    """Canonical bare id: drop the URL prefix and any trailing version."""
    a = (aid or "").strip()
    a = URL_PREFIX.sub("", a)
    a = VERSION_SUFFIX.sub("", a)
    return a


def version_of(aid: str):
    m = VERSION_SUFFIX.search((aid or "").strip())
    return int(m.group(1)) if m else None


def looks_like_categories(value: str) -> bool:
    """True for real arXiv category lists ('cs.CY; cs.AI'), False for 'influential'."""
    return bool(value) and "." in value


def merge_group(rows):
    """Return the merged field dict for a group of duplicate rows."""
    by_version = sorted(rows, key=lambda r: (r["version"] or 0, r["id"]))
    newest = by_version[-1]
    out = dict(newest)                       # newest text as the base
    out["id"] = min(r["id"] for r in rows)   # lowest id survives
    out["version"] = max((r["version"] or 0) for r in rows) or None
    out["published"] = min((r["published"] or "") for r in rows)
    out["ingested_at"] = min((r["ingested_at"] or "") for r in rows)
    out["citation_count"] = max((r["citation_count"] or 0) for r in rows)
    out["influential_citation_count"] = max((r["influential_citation_count"] or 0) for r in rows)
    out["deep_dived"] = max((r["deep_dived"] or 0) for r in rows)
    best = max(rows, key=lambda r: (r["relevance_score"] or 0))
    out["relevance_score"] = best["relevance_score"]
    out["relevance_categories"] = best["relevance_categories"]
    cat = next((r["categories"] for r in by_version[::-1] if looks_like_categories(r["categories"])),
               newest["categories"])
    out["categories"] = cat
    out["search_query"] = next((r["search_query"] for r in by_version[::-1] if r["search_query"]),
                               newest["search_query"])
    out["source"] = "influential" if any(r["source"] == "influential" for r in rows) else "fresh"
    for f in ("title", "summary", "authors", "links"):
        out[f] = newest[f] or next((r[f] for r in rows if r[f]), "")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    has_version = "version" in cols

    rows = [dict(r) for r in conn.execute("SELECT * FROM papers")]
    groups = {}
    for r in rows:
        r["version"] = r.get("version") or version_of(r["arxiv_id"])
        groups.setdefault(canon(r["arxiv_id"]), []).append(r)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    url_form = [r for r in rows if "://" in (r["arxiv_id"] or "")]
    vform = [r for r in rows if VERSION_SUFFIX.search((r["arxiv_id"] or "").strip())]
    surplus = sum(len(v) - 1 for v in dupes.values())
    already = [r for r in rows if r["arxiv_id"] == canon(r["arxiv_id"]) and not VERSION_SUFFIX.search(r["arxiv_id"])]

    print(f"rows                  : {len(rows)}")
    print(f"distinct papers       : {len(groups)}")
    print(f"URL-form ids          : {len(url_form)}   <- must become bare ids")
    print(f"ids carrying a version: {len(vform)}   <- version moves to its own column")
    print(f"duplicate groups      : {len(dupes)}   -> {surplus} surplus row(s) to merge away")
    print(f"already canonical     : {len(already)}")
    print(f"'version' column      : {'present' if has_version else 'WILL BE ADDED'}")

    if dupes:
        print("\nduplicate groups that merge:")
        for k, v in sorted(dupes.items())[:8]:
            vers = ", ".join(f"v{r['version']}" if r["version"] else "none" for r in v)
            print(f"  {canon(k):16s} {len(v)} rows ({vers})  ids={[r['id'] for r in v]}")
        if len(dupes) > 8:
            print(f"  ... and {len(dupes) - 8} more")

    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        return

    if not args.no_backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{DB_PATH}.bak-idnormalise-{stamp}"
        shutil.copy2(DB_PATH, backup)
        print(f"\nbackup: {backup}")

    if not has_version:
        conn.execute("ALTER TABLE papers ADD COLUMN version INTEGER")
        print("added column papers.version")

    merged = deleted = 0
    for key, grp in groups.items():
        if len(grp) == 1:
            continue
        m = merge_group(grp)
        dead = [r["id"] for r in grp if r["id"] != m["id"]]
        conn.executemany("DELETE FROM papers WHERE id = ?", [(i,) for i in dead])
        deleted += len(dead)
        conn.execute(
            """UPDATE papers SET arxiv_id=?, version=?, title=?, published=?, updated=?,
                   summary=?, authors=?, links=?, categories=?, search_query=?, ingested_at=?,
                   relevance_score=?, relevance_categories=?, citation_count=?,
                   influential_citation_count=?, deep_dived=?, source=?
               WHERE id=?""",
            (key, m["version"], m["title"], m["published"], m["updated"], m["summary"],
             m["authors"], m["links"], m["categories"], m["search_query"], m["ingested_at"],
             m["relevance_score"], m["relevance_categories"], m["citation_count"],
             m["influential_citation_count"], m["deep_dived"], m["source"], m["id"]))
        merged += 1
    conn.commit()
    print(f"merged {merged} group(s), removed {deleted} surplus row(s)")

    # Survivors of single-row groups still need their id canonicalised.
    fixed = 0
    for key, grp in groups.items():
        if len(grp) != 1:
            continue
        r = grp[0]
        if r["arxiv_id"] != key or r["version"] is None:
            conn.execute("UPDATE papers SET arxiv_id=?, version=? WHERE id=?",
                         (key, r["version"] or None, r["id"]))
            fixed += 1
    conn.commit()
    print(f"canonicalised {fixed} single-row id(s)")

    # The ledger is keyed on arxiv_id too, so it must move with the table.
    if conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                    "AND name='reported_papers'").fetchone()[0]:
        led = [dict(r) for r in conn.execute("SELECT * FROM reported_papers")]
        lg = {}
        for r in led:
            lg.setdefault(canon(r["arxiv_id"]), []).append(r)
        conn.execute("DELETE FROM reported_papers")
        for k, v in lg.items():
            conn.execute(
                """INSERT INTO reported_papers
                       (arxiv_id, first_reported_at, last_report_date, times_reported)
                   VALUES (?,?,?,?)""",
                (k, min(r["first_reported_at"] or "" for r in v),
                 max(r["last_report_date"] or "" for r in v),
                 max(r["times_reported"] or 1 for r in v)))
        conn.commit()
        print(f"ledger: {len(led)} rows -> {len(lg)} canonical key(s)")

    # Enforce it, so the next writer cannot reintroduce a second format.
    for op in ("INSERT", "UPDATE"):
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS papers_arxiv_id_canonical_{op.lower()}
            BEFORE {op} ON papers
            FOR EACH ROW
            WHEN NEW.arxiv_id LIKE '%://%'
              OR NEW.arxiv_id GLOB '*v[0-9]'
              OR NEW.arxiv_id GLOB '*v[0-9][0-9]'
            BEGIN
                SELECT RAISE(ABORT, 'papers.arxiv_id must be a canonical bare arXiv id: no URL prefix, no version suffix');
            END""")
    conn.commit()
    print("triggers installed on papers (INSERT + UPDATE)")

    conn.close()


if __name__ == "__main__":
    main()
