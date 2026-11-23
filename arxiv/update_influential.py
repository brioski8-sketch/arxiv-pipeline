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

import openalex_client

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "arxiv_papers.db")
QUERIES_PATH = os.path.join(BASE_DIR, "queries.json")
MARKER_FILE = os.path.join(BASE_DIR, ".last_influential")
MIN_DAYS_BETWEEN = 7
RELEVANCE_RESULTS = 25      # arXiv relevance hits to scan per query
KEEP_PER_QUERY = 6          # most-cited kept per query
MAILTO = os.environ.get("ARXIV_MAILTO") or "agentvi@agentmail.to"
USER_AGENT = f"HermesArxivBriefing/1.0 (mailto:{MAILTO})"

# arXiv API etiquette: identify with a contact address and keep to <=1 request
# per 3 seconds. See https://info.arxiv.org/help/api/tou.html
REQUEST_DELAY = 5.0           # seconds between arXiv queries (policy: >=3s)
HTTP_TIMEOUT = 20             # per-request; a hang here fails the query fast
MAX_ATTEMPTS = 4              # 1 try + 3 retries on ANY retryable failure
RETRY_BACKOFF = 10            # seconds, scaled by attempt number
# 2026-09-28: arXiv's edge answered HTTP 406 to every query for this step too.
# Retry the whole retryable class (406/5xx/transport), not just 429, and abort
# only when the API is genuinely down rather than on a transient hiccup.
RETRYABLE_STATUS = {406, 408, 425, 429, 500, 502, 503, 504}
MAX_CONSECUTIVE_FAILURES = 10  # abort once arXiv is really down, not on a blip
# Hard wall-clock budget for this script. It MUST finish in time for
# generate_report.py to run inside the wrapper's 600s cron cap — a throttled
# arXiv used to burn ~25 min here and kill the report with it. On overrun we
# stop enriching, store what we have, and do NOT stamp the marker (so the next
# run retries) instead of dying.
TIME_BUDGET = 300

# Set by main(); lets a retry sleep be clamped so it can never overrun the budget.
_DEADLINE = None
_LAST_ERROR = ""


def time_left():
    """Seconds until this script must stop, or inf when no deadline is set."""
    if _DEADLINE is None:
        return float("inf")
    return _DEADLINE - time.monotonic()

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
    """arXiv API relevance search over all history. Returns a list of paper dicts,
    or ``None`` when the query ultimately failed (so the caller can abort a
    throttled pull instead of grinding every query).

    Identifies via ``mailto=`` + User-Agent and retries any retryable failure
    (429/406/5xx/transport) with clamped backoff; inter-query spacing is the
    caller's job (main loop sleeps REQUEST_DELAY).
    """
    global _LAST_ERROR
    url = (
        f"https://export.arxiv.org/api/query?search_query={query_string}"
        f"&sortBy=relevance&sortOrder=descending&start=0&max_results={max_results}"
        f"&mailto={urllib.parse.quote(MAILTO)}"
    )
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
            print(f"  ERROR fetching {query_string[:60]}: {e}")
            _LAST_ERROR = last_err
            return None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_ATTEMPTS - 1 and time_left() > 0:
                wait = min(RETRY_BACKOFF * (attempt + 1), time_left())
                print(
                    f"  [{type(e).__name__}] transient, retrying in {wait:.0f}s "
                    f"(attempt {attempt + 2}/{MAX_ATTEMPTS})..."
                )
                time.sleep(wait)
                continue
            print(f"  ERROR fetching {query_string[:60]}: {e}")
            _LAST_ERROR = last_err
            return None
    if data is None:
        print(f"  ERROR fetching {query_string[:60]}: gave up after {MAX_ATTEMPTS} attempts ({last_err})")
        _LAST_ERROR = last_err
        return None

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


def store(conn, papers):
    """UPSERT: insert new papers as source='influential', backfill citations on existing.

    Returns (added, updated). Existence is checked BEFORE the upsert on purpose:
    SQLite reports rowcount == 1 for BOTH branches of INSERT ... ON CONFLICT DO
    UPDATE, so branching on rowcount silently reported every write as an insert and
    `updated` was permanently 0.
    """
    c = conn.cursor()
    added, updated, duplicates = 0, 0, 0
    for p in papers:
        # A genuine double-submission (same title AND authors under two arXiv
        # ids) is not catchable by id canonicalisation. Chouldechova 1610.07524
        # / 1703.00056 is the real example in this DB.
        dup = c.execute(
            "SELECT arxiv_id FROM papers WHERE lower(title) = ? AND lower(authors) = ? AND arxiv_id != ?",
            (p["title"].strip().lower(), p["authors"].strip().lower(), p["arxiv_id"]),
        ).fetchone()
        if dup:
            duplicates += 1
            continue
        existed = c.execute(
            "SELECT 1 FROM papers WHERE arxiv_id = ?", (p["arxiv_id"],)
        ).fetchone() is not None
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
        if existed:
            updated += 1
        else:
            added += 1
    conn.commit()
    return added, updated, duplicates


def main():
    global _DEADLINE
    print(f"=== Influential Papers Update: {datetime.datetime.now().isoformat()} ===")

    if not should_run():
        print("  Skipped — refreshed within the last 7 days.")
        print("ARXIV_INFLUENTIAL_RESULT: skipped=1 failed=0")
        return

    with open(QUERIES_PATH) as f:
        queries = json.load(f)

    started = time.monotonic()
    deadline = started + TIME_BUDGET
    _DEADLINE = deadline

    # 1. arXiv relevance search per query (all history)
    hits_by_query = {}
    consecutive_failures = 0
    failures = 0
    queries_run = 0
    for query_name, query_string in queries.items():
        if queries_run:
            time.sleep(REQUEST_DELAY)  # arXiv: <=1 request per 3s
        queries_run += 1

        hits = arxiv_relevance(query_string)
        if hits is None:
            consecutive_failures += 1
            failures += 1
            print(f"  {query_name}: FAILED [{consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}]")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(
                    f"  ABORT: {consecutive_failures} queries failed in a row after retries — "
                    f"arXiv appears to be down. Storing nothing new this run; the report "
                    f"still runs on existing data and the next run retries."
                )
                print(
                    f"ARXIV_INFLUENTIAL_RESULT: failed={failures} queries_run={queries_run} "
                    f"aborted=1 last_error={_LAST_ERROR or 'none'}"
                )
                return
            continue

        consecutive_failures = 0
        hits_by_query[query_name] = hits
        print(f"  {query_name}: {len(hits)} relevance hits")

    # 2. Enrich citations once per unique paper — bounded by TIME_BUDGET so a
    #    throttled OpenAlex can never push this script past the wrapper's cap.
    unique = {}
    for qname, hits in hits_by_query.items():
        for h in hits:
            unique.setdefault(h["arxiv_id"], h)
    print(f"  Enriching {len(unique)} unique papers via OpenAlex in batches of "
          f"{openalex_client.BATCH} (budget {TIME_BUDGET - (time.monotonic() - started):.0f}s)...")
    skipped = 0
    ids = list(unique.keys())
    for start in range(0, len(ids), openalex_client.BATCH):
        if time.monotonic() > deadline:
            skipped = len(ids) - start
            print(f"  Budget reached — skipping citation lookup for the remaining {skipped} papers.")
            break
        chunk = ids[start:start + openalex_client.BATCH]
        counts = openalex_client.fetch_batch(chunk, mailto=MAILTO, user_agent=USER_AGENT)
        if counts is None:
            skipped = len(ids) - start
            print(f"  OpenAlex batch failed at {start + 1}/{len(ids)}; leaving the rest unchecked.")
            break
        for aid in chunk:
            # A successful request checked every id in the chunk; absent from
            # the response means OpenAlex has no record yet -> 0 citations.
            unique[aid]["citation_count"] = counts.get(aid, 0)
            unique[aid]["query_name"] = None  # filled below
        print(f"    ...{min(start + openalex_client.BATCH, len(ids))}/{len(ids)}")

    # Unchecked papers stay without a citation_count (NOT 0) so the ranking
    # below can't mistake "never looked up" for "zero citations".
    for h in unique.values():
        h.setdefault("citation_count", None)

    # 3. Per query, keep the KEEP_PER_QUERY most-cited with > 0 citations
    conn = sqlite3.connect(DB_PATH)
    to_store = []
    for qname, hits in hits_by_query.items():
        ranked = sorted(
            (h for h in hits if (h.get("citation_count") or 0) > 0),
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

    added, updated, duplicates = store(conn, deduped)
    conn.close()

    if skipped:
        # Partial enrich only — leave the marker alone so the next run retries
        # the citation lookups instead of waiting out the 7-day window.
        print(f"  Marker NOT stamped ({skipped} papers left unenriched; next run retries).")
    else:
        with open(MARKER_FILE, "w") as f:
            f.write(datetime.datetime.now().isoformat())

    print(f"\n=== Done. {added} new influential papers added, {updated} existing papers' citations backfilled, {duplicates} duplicate title(s) skipped. ===")
    print(
        f"ARXIV_INFLUENTIAL_RESULT: added={added} backfilled={updated} failed={failures} "
        f"queries_run={queries_run} skipped_enrich={skipped} duplicates={duplicates} last_error={_LAST_ERROR or 'none'}"
    )


if __name__ == "__main__":
    main()
