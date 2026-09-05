#!/usr/bin/env python3
"""
Pull new papers from Arxiv API on topics relevant to the analyst's interests.
Search queries loaded from queries.json for easy external editing.
Runs via cron. Deduplicates by arxiv_id.
"""

import sqlite3
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import datetime
import os
import sys
import json

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "arxiv_papers.db")
RAW_DIR = os.path.join(BASE_DIR, "raw")
QUERIES_PATH = os.path.join(BASE_DIR, "queries.json")

MAX_RESULTS_PER_QUERY = 15

def load_queries():
    """Load search queries from JSON config file."""
    if not os.path.exists(QUERIES_PATH):
        print(f"ERROR: queries.json not found at {QUERIES_PATH}")
        sys.exit(1)
    with open(QUERIES_PATH) as f:
        queries = json.load(f)
    print(f"Loaded {len(queries)} search queries")
    return queries


def fetch_arxiv(query_name, query_string, max_results=MAX_RESULTS_PER_QUERY):
    """Fetch papers from Arxiv API."""
    url = f"https://export.arxiv.org/api/query?search_query={query_string}&sortBy=submittedDate&sortOrder=descending&start=0&max_results={max_results}"
    print(f"  Fetching: {query_name} ({url[:80]}...)")
    
    req = urllib.request.Request(url, headers={"User-Agent": "HermesAgent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  ERROR fetching {query_name}: {e}")
        return []
    
    os.makedirs(RAW_DIR, exist_ok=True)
    raw_path = os.path.join(RAW_DIR, f"{query_name}.xml")
    with open(raw_path, "w") as f:
        f.write(data)
    
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(data)
    
    papers = []
    for entry in root.findall("atom:entry", ns):
        arxiv_id = entry.find("atom:id", ns).text.strip()
        title = entry.find("atom:title", ns).text.strip().replace("\n", " ").replace("\r", "")
        published = entry.find("atom:published", ns).text.strip()[:10] if entry.find("atom:published", ns) is not None else ""
        updated = entry.find("atom:updated", ns).text.strip()[:10] if entry.find("atom:updated", ns) is not None else ""
        summary = entry.find("atom:summary", ns).text.strip().replace("\n", " ").replace("\r", "") if entry.find("atom:summary", ns) is not None else ""
        
        authors = "; ".join(
            a.find("atom:name", ns).text.strip()
            for a in entry.findall("atom:author", ns)
        )
        
        links = "; ".join(
            l.attrib.get("href", "")
            for l in entry.findall("atom:link", ns)
        )
        
        categories = "; ".join(
            c.attrib.get("term", "")
            for c in entry.findall("atom:category", ns)
        )
        
        papers.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "published": published,
            "updated": updated,
            "summary": summary,
            "authors": authors,
            "links": links,
            "categories": categories,
            "search_query": query_name,
        })
    
    print(f"  Found {len(papers)} papers for {query_name}")
    return papers


def store_papers(conn, papers):
    """Insert papers, skip duplicates by arxiv_id."""
    c = conn.cursor()
    
    c.execute("SELECT arxiv_id FROM papers")
    existing = set(r[0] for r in c.fetchall())
    
    new_count = 0
    for p in papers:
        if p["arxiv_id"] in existing:
            continue
        c.execute(
            """INSERT INTO papers (arxiv_id, title, published, updated, summary, authors, links, categories, search_query, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                p["arxiv_id"],
                p["title"],
                p["published"],
                p["updated"],
                p["summary"],
                p["authors"],
                p["links"],
                p["categories"],
                p["search_query"],
                datetime.datetime.now().isoformat(),
            ),
        )
        new_count += 1
        existing.add(p["arxiv_id"])
    
    conn.commit()
    return new_count


def main():
    print(f"=== Arxiv Update: {datetime.datetime.now().isoformat()} ===")
    
    queries = load_queries()
    
    conn = sqlite3.connect(DB_PATH)
    
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        arxiv_id TEXT UNIQUE,
        title TEXT,
        published TEXT,
        updated TEXT,
        summary TEXT,
        authors TEXT,
        links TEXT,
        categories TEXT,
        search_query TEXT,
        ingested_at TEXT
    )""")
    conn.commit()
    
    total_new = 0
    for query_name, query_string in queries.items():
        papers = fetch_arxiv(query_name, query_string)
        new_p = store_papers(conn, papers)
        total_new += new_p
    
    c.execute("SELECT COUNT(*) FROM papers")
    total = c.fetchone()[0]
    
    conn.close()
    
    print(f"\n=== Done. {total_new} new papers added. Total in DB: {total} ===")


if __name__ == "__main__":
    main()
