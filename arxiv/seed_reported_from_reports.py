#!/usr/bin/env python3
"""Seed the "reported once, never again" ledger from the briefing archive.

Every arxiv_id that has ever appeared in a briefing markdown file is a paper that
has already been reported, so it must not be reported again. The static top-cited
list carries titles but no ids, so those are resolved against the papers table.

Safety: every id is resolved against the DB before it is written, so the ledger
keys always match the keys `papers.arxiv_id` uses (and therefore match what
generate_report.py filters on). Version suffixes (v1, v2) are stripped when needed.

Usage:
    python3 seed_reported_from_reports.py --dry-run     # report only, write nothing
    python3 seed_reported_from_reports.py               # write the ledger
"""
import argparse
import datetime
import glob
import os
import re
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "arxiv_papers.db")
REPORTS = os.path.join(BASE, "reports")
LEDGER_TABLE = "reported_papers"

# `**Arxiv:** 2012.08713` / `**Arxiv:** http://arxiv.org/abs/2609.39066v1`
ARXIV_FIELD = re.compile(r"\*\*Arxiv:\*\*\s*(?:https?://arxiv\.org/abs/)?([A-Za-z0-9._/\-]+)")
# `  1. **Title** — 398 citations` inside the Top-Cited section
TOP_CITED_LINE = re.compile(r"^\s*\d+\.\s*\*\*(.+?)\*\*\s*—\s*\d+\s+citations\s*$", re.M)


def canon(aid: str) -> str:
    """Canonical paper identity: strip the URL prefix and any version suffix.

    Necessary because the DB stores MOST ids as full URLs
    (`http://arxiv.org/abs/2403.12108v3`, 5045 of 5194 rows) while the influential
    channel stores bare ids (`2403.12108`). The same paper therefore exists twice
    under different keys, and a citation in a briefing may use either form.
    """
    a = (aid or "").strip()
    a = re.sub(r"^https?://arxiv\.org/abs/", "", a)
    a = re.sub(r"v\d+$", "", a)
    return a


def build_index(conn):
    """canonical id -> set of every `arxiv_id` value in the DB for that paper."""
    idx = {}
    for (aid,) in conn.execute("SELECT arxiv_id FROM papers"):
        idx.setdefault(canon(aid), set()).add(aid)
    return idx


def resolve(index, raw: str):
    """Every DB arxiv_id for a cited reference (usually one, two if duplicated)."""
    return index.get(canon(raw), set())


def title_to_id(conn, title: str):
    row = conn.execute("SELECT arxiv_id FROM papers WHERE title = ? LIMIT 1", (title,)).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ledger-table", default=LEDGER_TABLE)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {args.ledger_table} (
        arxiv_id          TEXT PRIMARY KEY,
        first_reported_at TEXT,
        last_report_date  TEXT,
        times_reported    INTEGER DEFAULT 1)""")

    index = build_index(conn)
    files = sorted(glob.glob(os.path.join(REPORTS, "arxiv_briefing_*.md")))
    seen = {}          # arxiv_id -> [first_date, last_date, set_of_dates]
    unresolved = []
    papers = set()     # canonical ids, for an honest "distinct papers" count
    for path in files:
        date = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        date = date.group(1) if date else "?"
        text = open(path, encoding="utf-8", errors="ignore").read()
        ids = set()
        for raw in ARXIV_FIELD.findall(text):
            got = resolve(index, raw.strip().rstrip(".,);"))
            if got:
                ids |= got
            else:
                unresolved.append((date, raw))
        for title in TOP_CITED_LINE.findall(text):
            got = title_to_id(conn, title.strip())
            if got:
                ids.add(got)
            else:
                unresolved.append((date, f"title: {title[:60]}"))
        for aid in ids:
            papers.add(canon(aid))
            if aid not in seen:
                seen[aid] = [date, date, {date}]
            else:
                seen[aid][1] = date
                seen[aid][2].add(date)

    repeats = {k: v for k, v in seen.items() if len(v[2]) > 1}
    print(f"briefing files scanned : {len(files)}")
    print(f"ledger keys found      : {len(seen)}  (covering {len(papers)} distinct papers)")
    print(f"  of which REPEATED    : {len(repeats)}  (appeared in 2+ briefings)")
    print(f"unresolved references  : {len(unresolved)}")

    if repeats:
        print("\nmost-repeated papers:")
        for aid, (first, last, dates) in sorted(repeats.items(), key=lambda x: -len(x[1][2]))[:8]:
            t = conn.execute("SELECT title FROM papers WHERE arxiv_id = ?", (aid,)).fetchone()
            print(f"  {len(dates)}x  {aid[:44]:44s} {(t[0] if t else '?')[:44]}")

    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        conn.close()
        return

    now = datetime.datetime.now().isoformat(timespec="seconds")
    written = 0
    for aid, (first, last, dates) in seen.items():
        conn.execute(
            f"""INSERT INTO {args.ledger_table}
                    (arxiv_id, first_reported_at, last_report_date, times_reported)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(arxiv_id) DO UPDATE SET
                    last_report_date = excluded.last_report_date,
                    times_reported   = MAX(times_reported, excluded.times_reported)""",
            (aid, now, last, len(dates)))
        written += 1
    conn.commit()
    total = conn.execute(f"SELECT COUNT(*) FROM {args.ledger_table}").fetchone()[0]
    print(f"\nledger rows written: {written}  (table now holds {total})")
    conn.close()


if __name__ == "__main__":
    main()
