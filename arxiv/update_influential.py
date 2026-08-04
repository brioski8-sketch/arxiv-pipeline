#!/usr/bin/env python3
"""
Pull influential (most-cited) papers per interest topic.

Design:
- The fresh channel (update_arxiv.py) only pulls the NEWEST papers per query,
  so foundational/influential papers — the classics with hundreds of
  citations — can never enter the pool. This script adds them.
- Precision first: run each tuned arXiv query with sortBy=relevance over ALL
  history (not just recent). arXiv relevance returns the on-topic papers,
  old and new, the same way the fresh channel already works.
- Influence second: enrich the hits with citation counts via OpenAlex
  (10.48550/arXiv.{id} -> cited_by_count) and keep the top-6 most-cited per
  query. Zero-cite papers are skipped — the fresh channel covers new work.
- Papers already in the pool get their citation_count backfilled (UPSERT),
  which improves scoring everywhere.

Runs before generate_report.py in the pipeline. Skips itself if run within
the last 7 days (marker file) — top-cited lists barely change week to week.
"""

import json
import os
import re
import sqlite3
import time
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "arxiv_papers.db")
QUERIES_PATH = os.path.join(BASE_DIR, "queries.json")
MARKER_FILE = os.path.join(BASE_DIR, ".last_influential")
MIN_DAYS_BETWEEN = 7
RELEVANCE_RESULTS = 25      # arXiv relevance hits to scan per query
KEEP_PER_QUERY = 6          # most-cited kept per query
MAILTO = "brioski8@gmail.com"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def should_run():
    """Skip if we refreshed within MIN_DAYS_BETWEEN."""
    if not os.path.exists(MARKER_FILE):
        return True
    try:
        with open(MARKER_FILE) as f:
            last = datetime.datetime.fromisoformat(f.read().strip())
        return (datetime.datetime.now() - last).days >= MIN_DAYS_BETWEEN
    except Exception:
        return True


def arxiv_relevance(query_string, max_results=RELEVANCE_RESULTS):
    """arXiv API relevance search over all history. Returns list of paper dicts.

    arXiv export API throttles at ~1 req/3s — sleep between calls and retry
    on 429, otherwise back-to-back queries get rate-limited (the astronomy
    queries failed on first run for exactly this reason).
    """
    url = (
        f"https://export.arxiv.org/api/query?search_query={query_string}"
        f"&sortBy=relevance&sortOrder=descending&start=0&max_results={max_results}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "HermesAgent/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = 15 * (attempt + 1)
                print(f"  [429] rate limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"  ERROR fetching {query_string[:60]}: {e}")
                return []
        except Exception as e:
            print(f"  ERROR fetching {query_string[:60]}: {e}")
            return []
        finally:
            if attempt < 2:
                time.sleep(5)  # arXiv export API: ~1 req / 3s; 5s avoids sustained-burst 429s

    root = ET.fromstring(data)
    papers = []
    for entry in root.findall("atom:entry", NS):
        raw_id = entry.find("atom:id", NS).text.strip()
        m = re.search(r"(\d{4}\.\d{4,5})", raw_id)
        if not m:
            continue
        title = entry.find("atom:title", NS).text.strip().replace("\n", " ").replace("\r", "")
        published = entry.find("atom:published", NS).text.strip()[:10]
        summary = entry.find("atom:summary", NS).text.strip().replace("\n", " ").replace("\r", "")
        authors = "; ".join(
            a.find("atom:name", NS).text.strip()
            for a in entry.findall("atom:author", NS)[:10]
        )
        papers.append({
            "arxiv_id": m.group(1),
            "title": title,
            "published": published,
            "summary": summary,
            "authors": authors or "Unknown",
        })
    return papers


def fetch_cited_count(arxiv_id):
    """OpenAlex citation count for an arXiv paper. Returns int or 0."""
    url = (
        f"https://api.openalex.org/works/doi:10.48550/arXiv.{arxiv_id}"
        f"?select=cited_by_count&mailto={MAILTO}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "HermesAgent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data.get("cited_by_count", 0) or 0
    except Exception:
        return 0


def store(conn, papers):
    """UPSERT: insert new papers as source='influential', backfill citations on existing."""
    c = conn.cursor()
    added, updated = 0, 0
    for p in papers:
        c.execute(
            """INSERT INTO papers
               (arxiv_id, title, published, updated, summary, authors, links,
                categories, search_query, ingested_at, citation_count, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'influential')
               ON CONFLICT(arxiv_id) DO UPDATE SET
                   citation_count = excluded.citation_count,
                   summary = excluded.summary""",
            (
                p["arxiv_id"], p["title"], p["published"], p["published"],
                p["summary"], p["authors"],
                f"https://arxiv.org/abs/{p['arxiv_id']}",
                "influential", p["query_name"], datetime.datetime.now().isoformat(),
                p["citation_count"],
            ),
        )
        if c.rowcount == 1:
            added += 1
        else:
            updated += 1
    conn.commit()
    return added, updated


def main():
    print(f"=== Influential Papers Update: {datetime.datetime.now().isoformat()} ===")

    if not should_run():
        print("  Skipped — refreshed within the last 7 days.")
        return

    with open(QUERIES_PATH) as f:
        queries = json.load(f)

    # 1. arXiv relevance search per query (all history)
    hits_by_query = {}
    for query_name, query_string in queries.items():
        hits = arxiv_relevance(query_string)
        hits_by_query[query_name] = hits
        print(f"  {query_name}: {len(hits)} relevance hits")

    # 2. Enrich citations once per unique paper
    unique = {}
    for qname, hits in hits_by_query.items():
        for h in hits:
            unique.setdefault(h["arxiv_id"], h)
    print(f"  Enriching {len(unique)} unique papers via OpenAlex...")
    for i, (aid, h) in enumerate(unique.items()):
        h["citation_count"] = fetch_cited_count(aid)
        if (i + 1) % 25 == 0:
            print(f"    ...{i + 1}/{len(unique)}")
        h["query_name"] = None  # filled below

    # 3. Per query, keep the KEEP_PER_QUERY most-cited with > 0 citations
    conn = sqlite3.connect(DB_PATH)
    to_store = []
    for qname, hits in hits_by_query.items():
        ranked = sorted(
            (h for h in hits if h.get("citation_count", 0) > 0),
            key=lambda h: -h["citation_count"],
        )
        for h in ranked[:KEEP_PER_QUERY]:
            h["query_name"] = qname
            to_store.append(h)

    # Dedupe before store (same paper top-cited in multiple queries)
    seen_ids = set()
    deduped = []
    for h in sorted(to_store, key=lambda h: -h["citation_count"]):
        if h["arxiv_id"] not in seen_ids:
            seen_ids.add(h["arxiv_id"])
            deduped.append(h)

    added, updated = store(conn, deduped)
    conn.close()

    with open(MARKER_FILE, "w") as f:
        f.write(datetime.datetime.now().isoformat())

    print(f"\n=== Done. {added} new influential papers added, {updated} existing papers' citations backfilled. ===")


if __name__ == "__main__":
    main()
