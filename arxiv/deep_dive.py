#!/usr/bin/env python3
"""
Weekly Deep Dive script.

Takes the highest-scoring paper from the Arxiv DB that hasn't been deep-dived
yet, downloads its PDF, extracts full text, and generates a structured summary.

Usage:
    python3 deep_dive.py                         # system python3 has pymupdf

Output:
    - Full markdown report → reports/deep_dive_YYYY-MM-DD_{arxiv_id}.md
    - Short Telegram-friendly version → reports/short_deep_dive_YYYY-MM-DD_{arxiv_id}.txt
    - Both paths printed to stdout.
"""

import datetime
import os
import re
import sqlite3
import sys
import urllib.request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "arxiv_papers.db")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
TEMP_DIR = "/tmp"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FULL_TEXT_SAMPLE_CHARS = 3000
KEY_FINDING_PATTERNS = [
    r"we show",
    r"our results",
    r"we find",
    r"our approach",
    r"key contribution",
    r"main finding",
    r"we propose",
    r"our method",
]


def extract_arxiv_id(arxiv_url: str) -> str:
    """Extract the base arxiv ID from a URL like http://arxiv.org/abs/2605.04073v1."""
    # Try patterns: http://arxiv.org/abs/XXXX.XXXXX or https://arxiv.org/abs/XXXX.XXXXX
    m = re.search(r"arxiv\.org/abs/(\d+\.\d+)", arxiv_url)
    if m:
        return m.group(1)
    # Fall back to stripping version suffix
    m = re.search(r"arxiv\.org/abs/([\w.]+)", arxiv_url)
    if m:
        return m.group(1).rstrip("v").rstrip("0123456789")
    return arxiv_url.strip().split("/")[-1].split("v")[0]


def get_top_undived_paper(conn) -> tuple | None:
    """Fetch the highest-scoring undived paper, alternating channels by week.

    Alternates between the 'fresh' channel (recent papers) and the
    'influential' channel (most-cited classics) so the weekly deep dive
    surfaces important papers AND keeps up with new work. Falls back to the
    other channel when the preferred one is exhausted.
    """
    week_parity = datetime.date.today().isocalendar()[1] % 2
    preferred = "fresh" if week_parity == 0 else "influential"
    alternate = "influential" if preferred == "fresh" else "fresh"

    cur = conn.cursor()
    for source in (preferred, alternate):
        cur.execute(
            """SELECT id, arxiv_id, title, published, authors, summary,
                      relevance_score, relevance_categories, categories, links
               FROM papers
               WHERE deep_dived = 0 AND source = ?
               ORDER BY relevance_score DESC, id ASC
               LIMIT 1""",
            (source,),
        )
        row = cur.fetchone()
        if row:
            return row
    return None


def download_pdf(pdf_url: str, dest: str) -> bool:
    """Download a PDF from the given URL to the destination path."""
    print(f"  Downloading PDF from: {pdf_url}")
    try:
        req = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": "HermesAgent/1.0 (DeepDive)"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(dest, "wb") as f:
                f.write(resp.read())
        size = os.path.getsize(dest)
        print(f"  Downloaded {size} bytes to {dest}")
        return True
    except Exception as e:
        print(f"  ERROR downloading PDF: {e}")
        return False


def extract_text_with_pymupdf(pdf_path: str) -> str | None:
    """Extract text from a PDF using PyMuPDF (fitz)."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        print(f"  Opened PDF: {doc.page_count} pages")
        texts = []
        for i, page in enumerate(doc):
            text = page.get_text("text")
            texts.append(text)
        doc.close()
        full_text = "\n\n".join(texts)
        print(f"  Extracted {len(full_text)} characters of text")
        return full_text
    except ImportError:
        print("  ERROR: PyMuPDF (fitz) is not installed.")
        return None
    except Exception as e:
        print(f"  ERROR extracting text: {e}")
        return None


def extract_key_findings(full_text: str) -> list[str]:
    """Scan the full text for sentences containing key finding patterns."""
    findings = []
    # Split into sentences (simple approach)
    sentences = re.split(r"(?<=[.!?])\s+", full_text)
    for sentence in sentences:
        sentence_lower = sentence.strip().lower()
        for pattern in KEY_FINDING_PATTERNS:
            if re.search(pattern, sentence_lower):
                # Clean up the sentence
                clean = sentence.strip()
                if len(clean) > 30 and clean not in findings:
                    findings.append(clean)
                break  # one pattern match per sentence is enough
    # Deduplicate and limit
    seen = set()
    unique_findings = []
    for f in findings:
        key = f[:100].lower()
        if key not in seen:
            seen.add(key)
            unique_findings.append(f)
    return unique_findings[:15]


def generate_relevance_notes(
    relevance_score: int, relevance_categories: str | None, categories: str | None
) -> str:
    """Generate a short relevance explanation."""
    notes = []
    if relevance_score and relevance_score > 0:
        notes.append(f"Relevance score: {relevance_score}")
    if relevance_categories:
        notes.append(f"Matched interests: {relevance_categories}")
    if categories:
        notes.append(f"Arxiv categories: {categories}")
    if not notes:
        notes.append("Paper in the tracked search pipeline.")
    return " | ".join(notes)


def generate_report(
    paper: tuple, full_text: str | None, key_findings: list[str], arxiv_id: str
) -> tuple[str, str]:
    """Generate full markdown report and short Telegram version.

    paper tuple: (id, arxiv_id, title, published, authors, summary,
                   relevance_score, relevance_categories, categories, links)
    """
    (
        p_id,
        p_arxiv_id,
        p_title,
        p_published,
        p_authors,
        p_summary,
        p_relevance_score,
        p_relevance_categories,
        p_categories,
        p_links,
    ) = paper

    today = datetime.date.today().isoformat()

    # Full abstract
    abstract = p_summary if p_summary else "No abstract available."

    # Full text sample
    if full_text:
        # Remove the abstract from full text if it starts with it
        body = full_text
        if p_summary and p_summary[:100] in full_text:
            # Try to skip past the abstract section
            idx = full_text.find(p_summary[:100])
            if idx >= 0:
                body = full_text[idx + len(p_summary) :].strip()
        full_text_sample = body[:FULL_TEXT_SAMPLE_CHARS]
        if len(body) > FULL_TEXT_SAMPLE_CHARS:
            full_text_sample += "..."
    else:
        full_text_sample = "Full text extraction failed."

    # Relevance
    relevance_notes = generate_relevance_notes(
        p_relevance_score, p_relevance_categories, p_categories
    )

    # Build PDF URL from arxiv_id
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    # Find the arxiv abstract URL
    if p_links:
        abs_url = p_links.split(";")[0].strip()
    else:
        abs_url = f"https://arxiv.org/abs/{arxiv_id}"

    # ---------- Full Markdown Report ----------
    md = f"""# Deep Dive: {p_title}

**Date:** {today}
**Arxiv ID:** {arxiv_id}
**Abstract URL:** [{abs_url}]({abs_url})
**PDF URL:** [{pdf_url}]({pdf_url})

---

## TITLE
{p_title}

## AUTHORS
{p_authors if p_authors else "Not listed"}

## PUBLISHED
{p_published if p_published else "Unknown"}

## ABSTRACT
{abstract}

## FULL_TEXT_SAMPLE
```
{full_text_sample}
```

## KEY_FINDINGS
"""

    if key_findings:
        for i, finding in enumerate(key_findings, 1):
            # Truncate very long findings
            f_text = finding[:500] + "..." if len(finding) > 500 else finding
            md += f"{i}. {f_text}\n\n"
    else:
        md += "No key findings automatically extracted.\n\n"

    md += f"""## RELEVANCE
{relevance_notes}

---

*Generated by Hermes Agent Deep Dive on {today}*
"""

    # ---------- Short Telegram Version ----------
    short_lines = [
        f"**Deep Dive:** {p_title}",
        f"**Authors:** {p_authors[:120] if p_authors else 'Not listed'}",
        f"**Published:** {p_published if p_published else 'Unknown'}",
        f"**Arxiv:** {abs_url}",
        "",
        "**Abstract:**",
        abstract[:500] + ("..." if len(abstract) > 500 else ""),
    ]

    if key_findings:
        short_lines.append("")
        short_lines.append("**Key Findings:**")
        for finding in key_findings[:5]:
            short_lines.append(f"• {finding[:200]}")
    else:
        short_lines.append("")
        short_lines.append("*(No key findings auto-extracted)*")

    short_lines.append("")
    short_lines.append(f"**Relevance:** {relevance_notes}")

    short_text = "\n".join(short_lines)

    return md, short_text


def mark_deep_dived(conn, paper_id: int):
    """Mark a paper as deep-dived in the database."""
    cur = conn.cursor()
    cur.execute("UPDATE papers SET deep_dived = 1 WHERE id = ?", (paper_id,))
    conn.commit()
    print(f"  Marked paper id={paper_id} as deep_dived=1")


def main():
    print(f"=== Arxiv Deep Dive: {datetime.datetime.now().isoformat()} ===\n")

    # Connect to DB
    conn = sqlite3.connect(DB_PATH)

    # 1. Find the top undived paper
    paper = get_top_undived_paper(conn)
    if not paper:
        print("No undived papers found. All caught up!")
        conn.close()
        return

    p_id, p_arxiv_id, p_title, p_published, p_authors, p_summary = paper[:6]
    print(f"Selected paper: \"{p_title}\"")
    print(f"  ID: {p_id} | Published: {p_published} | Relevance: {paper[6]}")

    # 2. Extract clean arxiv ID
    arxiv_id = extract_arxiv_id(p_arxiv_id)
    print(f"  Extracted arxiv_id: {arxiv_id}")

    # 3. Download PDF
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    pdf_path = os.path.join(TEMP_DIR, f"{arxiv_id}.pdf")
    success = download_pdf(pdf_url, pdf_path)

    # 4. Extract text
    full_text = None
    if success and os.path.exists(pdf_path):
        full_text = extract_text_with_pymupdf(pdf_path)

    # 5. Extract key findings
    key_findings = []
    if full_text:
        key_findings = extract_key_findings(full_text)
        print(f"  Found {len(key_findings)} key finding sentences")

    # 6. Generate reports
    md_report, short_report = generate_report(
        paper, full_text, key_findings, arxiv_id
    )

    # 7. Save reports
    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()

    md_path = os.path.join(REPORTS_DIR, f"deep_dive_{today}_{arxiv_id}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"\n  Full report saved: {md_path}")

    short_path = os.path.join(
        REPORTS_DIR, f"short_deep_dive_{today}_{arxiv_id}.txt"
    )
    with open(short_path, "w", encoding="utf-8") as f:
        f.write(short_report)
    print(f"  Short report saved: {short_path}")

    # 8. Mark as deep-dived
    mark_deep_dived(conn, p_id)

    # 9. Clean up temp PDF
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
        print(f"  Cleaned up temp PDF: {pdf_path}")

    conn.close()

    print(f"\n=== Deep Dive complete. ===")
    print(f"Full report:  {md_path}")
    print(f"Short report: {short_path}")


if __name__ == "__main__":
    main()
