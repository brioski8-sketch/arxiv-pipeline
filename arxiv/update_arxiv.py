#!/usr/bin/env python3
"""
Pull new papers from Arxiv API on topics relevant to the analyst's interests.
Search queries loaded from queries.json for easy external editing.
Runs via cron. Deduplicates by arxiv_id.
"""

import sqlite3
import re
import time
import urllib.error
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

# --- arXiv API etiquette (https://info.arxiv.org/help/api/tou.html) ---
# arXiv asks that automated clients (a) identify themselves with a contact
# address and (b) keep to <=1 request per 3 seconds. Anonymous back-to-back
# bursts get HTTP-429 throttled at the edge — which is exactly what killed the
# 2026-09-14 run (28/28 queries 429'd, then the pull kept grinding).
MAILTO = os.environ.get("ARXIV_MAILTO") or "agentvi@agentmail.to"
USER_AGENT = f"HermesArxivBriefing/1.0 (mailto:{MAILTO})"
REQUEST_DELAY = 3.5           # seconds between arXiv queries (policy: >=3s)
HTTP_TIMEOUT = 20             # per-request; a hang here fails the query fast
MAX_ATTEMPTS = 4              # 1 try + 3 retries on ANY retryable failure
RETRY_BACKOFF = 10            # seconds, scaled by attempt number
# 2026-09-28: arXiv's edge answered HTTP 406 to every query in the 09:00 window,
# killing the whole 28-query pull and producing a silent zero-paper week. The
# lesson is that 406/5xx/transport errors are TRANSIENT and must be retried —
# only a sustained run of failures means the API is actually down. Retry the
# whole retryable class, not just 429, and abort far later than 3.
RETRYABLE_STATUS = {406, 408, 425, 429, 500, 502, 503, 504}
MAX_CONSECUTIVE_FAILURES = 10  # abort only when arXiv is hard-down, not on a hiccup
PULL_TIME_BUDGET = 240        # wall-clock cap on the pull; retries respect it

# Set by main(); lets a retry sleep be clamped so it can never overrun the budget.
_PULL_DEADLINE = None
_LAST_ERROR = ""


def time_left():
    """Seconds until the pull must stop, or inf when no deadline is set."""
    if _PULL_DEADLINE is None:
        return float("inf")
    return _PULL_DEADLINE - time.monotonic()

def load_queries():
    """Load search queries from JSON config file."""
    if not os.path.exists(QUERIES_PATH):
        print(f"ERROR: queries.json not found at {QUERIES_PATH}")
        sys.exit(1)
    with open(QUERIES_PATH) as f:
        queries = json.load(f)
    print(f"Loaded {len(queries)} search queries")
    return queries


def canonical_arxiv_id(raw_id: str):
    """Split a raw arXiv <id> into the canonical bare id and its version.

        'http://arxiv.org/abs/2403.12108v3'  -> ('2403.12108', 3)
        'https://arxiv.org/abs/hep-th/0601001' -> ('hep-th/0601001', None)
        '2403.12108'                         -> ('2403.12108', None)

    `arxiv_id` is a UNIQUE key, so it must hold the bare id with the version kept in
    its own column. Keeping the version inside the key makes a later arXiv version
    insert a DUPLICATE row for the same paper instead of updating the existing one
    (measured 2026-10-05: 45 such duplicate pairs). A DB trigger rejects any
    non-canonical value written to papers.arxiv_id, so this must run before insert.
    """
    raw_id = (raw_id or "").strip()
    m = re.search(r"v(\d+)$", raw_id)
    version = int(m.group(1)) if m else None
    aid = re.sub(r"^https?://arxiv\.org/abs/", "", raw_id)
    aid = re.sub(r"v\d+$", "", aid)
    return aid, version


def fetch_arxiv(query_name, query_string, max_results=MAX_RESULTS_PER_QUERY):
    """Fetch papers from Arxiv API.

    Identifies itself via ``mailto=`` (query string AND User-Agent) and retries
    any retryable failure (429/406/5xx/transport) with clamped backoff. Returns
    ``None`` — not ``[]`` — when the query ultimately failed, so the caller can
    count consecutive failures and stop a genuinely dead pull instead of burning
    the whole budget on 28 doomed requests.
    """
    global _LAST_ERROR
    url = (
        f"https://export.arxiv.org/api/query?search_query={query_string}"
        f"&sortBy=submittedDate&sortOrder=descending&start=0&max_results={max_results}"
        f"&mailto={urllib.parse.quote(MAILTO)}"
    )
    print(f"  Fetching: {query_name} ({url[:80]}...)")

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    data = None
    last_err = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            retryable = e.code in RETRYABLE_STATUS
            if retryable and attempt < MAX_ATTEMPTS - 1 and time_left() > 0:
                wait = min(RETRY_BACKOFF * (attempt + 1), time_left())
                print(
                    f"  [{e.code}] transient, retrying in {wait:.0f}s "
                    f"(attempt {attempt + 2}/{MAX_ATTEMPTS})..."
                )
                time.sleep(wait)
                continue
            print(f"  ERROR fetching {query_name}: {e}")
            _LAST_ERROR = last_err
            return None
        except Exception as e:
            # Transport-level failure (timeout / DNS / reset) — same treatment as 5xx:
            # a flaky 20s window must not cost the whole week's pull.
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_ATTEMPTS - 1 and time_left() > 0:
                wait = min(RETRY_BACKOFF * (attempt + 1), time_left())
                print(
                    f"  [{type(e).__name__}] transient, retrying in {wait:.0f}s "
                    f"(attempt {attempt + 2}/{MAX_ATTEMPTS})..."
                )
                time.sleep(wait)
                continue
            print(f"  ERROR fetching {query_name}: {e}")
            _LAST_ERROR = last_err
            return None
    if data is None:
        print(f"  ERROR fetching {query_name}: gave up after {MAX_ATTEMPTS} attempts ({last_err})")
        _LAST_ERROR = last_err
        return None

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
        # The Atom <id> is a full URL that carries a version; canonicalise it.
        raw_id = entry.find("atom:id", ns).text.strip()
        arxiv_id, version = canonical_arxiv_id(raw_id)
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
            "version": version,
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
            """INSERT INTO papers (arxiv_id, title, published, updated, summary, authors, links, categories, search_query, ingested_at, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                p.get("version"),
            ),
        )
        new_count += 1
        existing.add(p["arxiv_id"])
    
    conn.commit()
    return new_count


def main():
    global _PULL_DEADLINE
    print(f"=== Arxiv Update: {datetime.datetime.now().isoformat()} ===")
    print(f"  Retry policy: {MAX_ATTEMPTS} attempts, retryable={sorted(RETRYABLE_STATUS)}, "
          f"pull budget {PULL_TIME_BUDGET}s")
    _PULL_DEADLINE = time.monotonic() + PULL_TIME_BUDGET
    
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
        ingested_at TEXT,
        version INTEGER
    )""")
    conn.commit()
    
    total_new = 0
    consecutive_failures = 0
    failures = 0
    queries_run = 0
    for query_name, query_string in queries.items():
        if queries_run:
            time.sleep(REQUEST_DELAY)  # arXiv: <=1 request per 3s
        queries_run += 1

        papers = fetch_arxiv(query_name, query_string)
        if papers is None:
            consecutive_failures += 1
            failures += 1
            print(f"  [{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES} consecutive failures]")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(
                    f"  ABORT: {consecutive_failures} queries failed in a row after retries — "
                    f"arXiv appears to be down. Stopping the pull early and keeping the "
                    f"{total_new} papers ingested so far (next run picks up the rest)."
                )
                break
            continue

        consecutive_failures = 0
        new_p = store_papers(conn, papers)
        total_new += new_p
    
    c.execute("SELECT COUNT(*) FROM papers")
    total = c.fetchone()[0]
    
    conn.close()
    
    print(f"\n=== Done. {total_new} new papers added. Total in DB: {total} ===")
    # Machine-readable summary. run_arxiv_pipeline.sh greps this line to tell a
    # genuinely quiet week (failed=0) from a dead pull (failed>0) — the two used
    # to look identical, which is how a total collection failure went unnoticed.
    print(
        f"ARXIV_PULL_RESULT: new={total_new} failed={failures} "
        f"queries_run={queries_run} last_error={_LAST_ERROR or 'none'}"
    )


if __name__ == "__main__":
    main()
