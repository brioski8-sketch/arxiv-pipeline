#!/usr/bin/env python3
"""Build a local SQLite database of arXiv preprints related to criminal justice.

.. deprecated::
    SUPERSEDED by the maintained pipeline in ``~/.hermes/datasets/arxiv/``
    (``update_arxiv.py`` + ``update_influential.py`` + ``generate_report.py``,
    driven by cron job ``aaa298d8bf41`` via ``run_arxiv_pipeline.sh``).
    Nothing scheduled calls this script — it is kept only for reference.

    If you DO run it, it now follows the arXiv Terms of Use
    (https://info.arxiv.org/help/api/tou.html): identifying mailto in the query
    and User-Agent, >=3s between requests, and no retry-storming on 429.
    It previously slept 0.5s ("be nice to arXiv") — that was 6x over the limit
    and a genuine way to earn a rate-limit block.
"""

import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import sqlite3
import os
import json
from datetime import datetime
import time

DB_PATH = os.path.expanduser("~/.hermes/datasets/arxiv/arxiv_papers.db")
DATA_DIR = os.path.expanduser("~/.hermes/datasets/arxiv/raw")
os.makedirs(DATA_DIR, exist_ok=True)

# Queries relevant to criminal justice, criminology, and law enforcement
SEARCH_QUERIES = [
    "criminal+justice",
    "criminology+recidivism",
    "sentencing+algorithm",
    "police+data+analysis",
    "fairness+justice+algorithm",
    "predictive+policing",
    "recidivism+prediction",
    "bail+risk+assessment",
    "court+automation",
    "crime+forecasting",
    "restorative+justice",
    "legal+reasoning+NLP",
]

# --- arXiv API etiquette (https://info.arxiv.org/help/api/tou.html) ---
# Identify with a contact address; keep to <=1 request per 3 seconds.
MAILTO = os.environ.get("ARXIV_MAILTO") or "agentvi@agentmail.to"
USER_AGENT = f"HermesArxivBriefing/1.0 (mailto:{MAILTO})"
REQUEST_DELAY = 3.5
HTTP_TIMEOUT = 20

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/"
}

def fetch_arxiv(query, max_results=10):
    """Search the arXiv API for a given query (ToU-compliant: mailto + >=3s spacing)."""
    url = (
        f"https://export.arxiv.org/api/query?search_query=all:{query}"
        f"&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"
        f"&mailto={urllib.parse.quote(MAILTO)}"
    )
    time.sleep(REQUEST_DELAY)  # arXiv ToU: no more than one request every 3 seconds
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        # Do NOT retry-storm a 429: that is the behaviour the ToU warns about.
        raise RuntimeError(f"arXiv HTTP {e.code} (rate limited?)") from e

def parse_entry(entry):
    """Parse an arXiv Atom entry into a dict."""
    title = entry.find("atom:title", NS).text.strip().replace('\n', ' ').replace('\r', '')
    published = entry.find("atom:published", NS).text.strip()
    updated = entry.find("atom:updated", NS).text.strip()
    
    summary = entry.find("atom:summary", NS).text.strip().replace('\n', ' ').replace('\r', '')
    
    # Authors
    authors = []
    for author in entry.findall("atom:author", NS):
        name = author.find("atom:name", NS).text.strip()
        authors.append(name)
    
    # arXiv ID
    arxiv_id = entry.find("atom:id", NS).text.strip()
    
    # Links
    links = []
    for link in entry.findall("atom:link", NS):
        links.append(link.attrib.get("href", ""))
    
    # Categories / tags
    categories = []
    for cat in entry.findall("atom:category", NS):
        categories.append(cat.attrib.get("term", ""))
    
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "published": published[:10],
        "updated": updated[:10],
        "summary": summary[:500],
        "authors": "; ".join(authors),
        "links": "; ".join(links),
        "categories": "; ".join(categories)
    }

def build_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS papers (
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
        )
    """)
    
    all_papers = []
    seen_ids = set()
    
    for query in SEARCH_QUERIES:
        print(f"Searching: {query}...")
        try:
            raw = fetch_arxiv(query)
            root = ET.fromstring(raw)
            entries = root.findall("atom:entry", NS)
            
            query_results = []
            for entry in entries:
                paper = parse_entry(entry)
                if paper["arxiv_id"] not in seen_ids:
                    seen_ids.add(paper["arxiv_id"])
                    query_results.append(paper)
            
            all_papers.extend(query_results)
            print(f"  {len(query_results)} new papers found")
            
            # Save raw XML for reference
            safe_name = query.replace("+", "_").replace("-", "_")
            with open(os.path.join(DATA_DIR, f"{safe_name}.xml"), "w") as f:
                f.write(raw)
                
        except Exception as e:
            print(f"  Error: {e}")
    
    # Insert into DB
    now = datetime.now().isoformat()
    for paper in all_papers:
        try:
            c.execute(
                "INSERT OR IGNORE INTO papers (arxiv_id, title, published, updated, summary, authors, links, categories, search_query, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (paper["arxiv_id"], paper["title"], paper["published"], paper["updated"], paper["summary"], paper["authors"], paper["links"], paper["categories"], paper.get("search_query", "multiple"), now)
            )
        except Exception as e:
            print(f"  Error inserting {paper['arxiv_id']}: {e}")
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_papers_categories ON papers(categories)")
    
    conn.commit()
    
    # Count
    c.execute("SELECT COUNT(*) FROM papers")
    count = c.fetchone()[0]
    
    conn.close()
    print(f"\nDatabase: {DB_PATH}")
    print(f"Total unique papers: {count}")

if __name__ == "__main__":
    build_db()
