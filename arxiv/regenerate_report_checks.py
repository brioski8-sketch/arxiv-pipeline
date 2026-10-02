#!/usr/bin/env python3
"""Regenerate one paper's deep-dive report from an already-downloaded PDF.

Fixes reports where full-text extraction failed (e.g. wrong interpreter
without PyMuPDF). Run with /usr/bin/python3.

Usage: /usr/bin/python3 regenerate_report_checks.py <paper_id> <pdf_path>
"""
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deep_dive as dd

paper_id = int(sys.argv[1])
pdf_path = sys.argv[2]

conn = sqlite3.connect(dd.DB_PATH)
cur = conn.cursor()
cur.execute(
    """SELECT id, arxiv_id, title, published, authors, summary,
              relevance_score, relevance_categories, categories, links
       FROM papers WHERE id = ?""",
    (paper_id,),
)
paper = cur.fetchone()
if not paper:
    sys.exit(f"paper id {paper_id} not found")

arxiv_id = dd.extract_arxiv_id(paper[1])
full_text = dd.extract_text_with_pymupdf(pdf_path)
key_findings = dd.extract_key_findings(full_text) if full_text else []
print(f"  Found {len(key_findings)} key finding sentences")

md_report, short_report = dd.generate_report(paper, full_text, key_findings, arxiv_id)

today = datetime.date.today().isoformat()
md_path = os.path.join(dd.REPORTS_DIR, f"deep_dive_{today}_{arxiv_id}.md")
short_path = os.path.join(dd.REPORTS_DIR, f"short_deep_dive_{today}_{arxiv_id}.txt")
with open(md_path, "w", encoding="utf-8") as f:
    f.write(md_report)
with open(short_path, "w", encoding="utf-8") as f:
    f.write(short_report)

# Save full text for downstream use
ft_path = os.path.join(dd.REPORTS_DIR, f"fulltext_{arxiv_id}.txt")
with open(ft_path, "w", encoding="utf-8") as f:
    f.write(full_text or "")

print(f"  Full report:  {md_path}")
print(f"  Short report: {short_path}")
print(f"  Full text:    {ft_path} ({len(full_text or '')} chars)")
conn.close()
