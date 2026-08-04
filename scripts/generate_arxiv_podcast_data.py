#!/usr/bin/env python3
"""
Extract top 5 most relevant new arxiv papers for podcast generation.
Outputs structured JSON for the agent to use in podcast script writing.

Uses the same .last_report cutoff as the main pipeline so it reports
on the same set of papers the briefing just covered.
"""

import sqlite3
import json
import os
import datetime
import re
import glob

DB_PATH = os.path.expanduser("~/.hermes/datasets/arxiv/arxiv_papers.db")
REPORTS_DIR = os.path.expanduser("~/.hermes/datasets/arxiv/reports")
LAST_RUN_FILE = os.path.expanduser("~/.hermes/datasets/arxiv/.last_report")
OUTPUT_FILE = os.path.expanduser("~/.hermes/datasets/arxiv/podcast/podcast_papers.json")


def get_report_cutoff():
    """Same cutoff logic as generate_report.py — papers since last pipeline run."""
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE) as f:
            ts = f.read().strip()
            if ts:
                return ts
    # Fallback: most recent report
    reports = sorted(glob.glob(os.path.join(REPORTS_DIR, "short_*.txt")), reverse=True)
    if reports:
        m = re.search(r'short_(\d{4}-\d{2}-\d{2})\.txt', reports[0])
        if m:
            return m.group(1) + "T00:00:00"
    return (datetime.datetime.now() - datetime.timedelta(hours=48)).isoformat()


def clean_text(text):
    """Strip LaTeX math and clean up text for TTS."""
    if not text:
        return ""
    # Remove LaTeX math \(...\) and \[...\]
    text = re.sub(r'\\\(.*?\\\)', '', text, flags=re.DOTALL)
    text = re.sub(r'\\\[.*?\\\]', '', text, flags=re.DOTALL)
    # Remove $...$ math
    text = re.sub(r'\$[^$]+\$', '', text)
    # Remove multiple spaces
    text = re.sub(r'\s+', ' ', text)
    # Remove leading/trailing whitespace
    text = text.strip()
    return text


def extract_papers():
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    cutoff = get_report_cutoff()
    print(f"Cutoff: {cutoff}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get papers ingested since cutoff, scored by relevance
    c.execute("""
        SELECT title, summary, authors, published, arxiv_id, categories,
               relevance_score, relevance_categories, citation_count
        FROM papers
        WHERE ingested_at >= ?
        ORDER BY relevance_score DESC, published DESC
        LIMIT 10
    """, (cutoff,))
    rows = [dict(r) for r in c.fetchall()]

    # If no new scored papers, fall back to most recent 5
    if not rows:
        c.execute("""
            SELECT title, summary, authors, published, arxiv_id, categories,
                   relevance_score, relevance_categories, citation_count
            FROM papers
            ORDER BY ingested_at DESC
            LIMIT 5
        """)
        rows = [dict(r) for r in c.fetchall()]

    # Clean up summaries for TTS readability
    papers = []
    for r in rows:
        papers.append({
            "title": clean_text(r["title"]),
            "summary": clean_text(r["summary"]),
            "authors": clean_text(r["authors"][:200]) if r["authors"] else "Unknown",
            "published": r["published"][:10] if r["published"] else "Unknown",
            "arxiv_id": r["arxiv_id"],
            "categories": r["categories"],
            "relevance_score": r["relevance_score"] or 0,
            "relevance_categories": r["relevance_categories"] or "",
            "citation_count": r["citation_count"] or 0,
        })

    # Also get DB stats for context
    c.execute("SELECT COUNT(*) FROM papers")
    total_count = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT search_query) FROM papers")
    query_count = c.fetchone()[0]

    conn.close()

    output = {
        "generated_at": datetime.datetime.now().isoformat(),
        "cutoff": cutoff,
        "total_papers_db": total_count,
        "search_topics": query_count,
        "papers": papers[:5],  # Top 5 for podcast
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Extracted {len(papers[:5])} papers for podcast.")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Output JSON:\n{json.dumps(output, indent=2)}")


if __name__ == "__main__":
    extract_papers()
