#!/usr/bin/env python3
"""
Generate the personalized Arxiv research briefing.
Score papers using the 7-category relevance engine from INTERESTS.md.
Enriches citation data from Semantic Scholar at report time.
"""

import sqlite3
import datetime
import os
import re
import json
import time
import math
import glob
import urllib.request
import urllib.error

DB_PATH = os.path.join(os.path.dirname(__file__), "arxiv_papers.db")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "reports")
LAST_RUN_FILE = os.path.join(os.path.dirname(__file__), ".last_report")

# Size of the briefing window, in days, counted back from midnight today. The
# pipeline runs weekly, so this must span a full week of ingestion — a narrower
# window silently drops papers that were pulled during the previous run.
REPORT_WINDOW_DAYS = 7

# API etiquette: identify the client with a contact address. OpenAlex grants its
# "polite pool" (better rate limits) when a mailto is supplied; Semantic Scholar
# and arXiv expect the same courtesy. Anonymous clients are throttled first.
MAILTO = os.environ.get("ARXIV_MAILTO") or "agentvi@agentmail.to"
USER_AGENT = f"HermesArxivBriefing/1.0 (mailto:{MAILTO})"


# ── "Reported once, never again" ledger ──────────────────────────────────────
# A paper that has appeared in a briefing must not appear in a later one. Without
# this, the static no-window queries — the all-time top-cited list especially —
# print identical papers in EVERY briefing. Measured 2026-10-05: the same five
# papers, all with ingested_at of 2026-08-02, appeared verbatim in the 21 Sep,
# 28 Sep and 5 Oct briefings, and "A Collection of Definitions of Intelligence"
# (arXiv 0706.3639) was in every one of them.
#
# Keyed on arxiv_id, which is UNIQUE in `papers`. A separate table rather than a
# new column on `papers`, so this can be inspected, seeded and cleared on its own
# without touching the ingestion schema.
LEDGER_TABLE = "reported_papers"


def ensure_ledger(conn) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
        arxiv_id          TEXT PRIMARY KEY,
        first_reported_at TEXT,
        last_report_date  TEXT,
        times_reported    INTEGER DEFAULT 1)""")
    conn.commit()


def load_reported(conn, before: str | None = None) -> set:
    """arxiv_ids already reported, optionally only those reported BEFORE a date.

    `before` exists to make a same-day re-run idempotent. The briefing is generated
    TWICE per run: once by run_arxiv_pipeline.sh, then again by the cron prompt's
    Step 1 (`python3 generate_report.py`). Without this, the second run would treat
    everything the first run just displayed as already-reported, suppress all of it,
    and overwrite the briefing with an empty report — leaving the podcast nothing to
    cover. Passing today's date means only PREVIOUS days suppress.
    """
    ensure_ledger(conn)
    try:
        if before:
            return {r[0] for r in conn.execute(
                f"SELECT arxiv_id FROM {LEDGER_TABLE} "
                f"WHERE last_report_date IS NULL OR last_report_date < ?", (before,))}
        return {r[0] for r in conn.execute(f"SELECT arxiv_id FROM {LEDGER_TABLE}")}
    except sqlite3.Error:
        return set()


def arxiv_url(paper) -> str:
    """Canonical link for a paper.

    `arxiv_id` is a bare id (no URL, no version), so the link must be constructed.
    Before the id was normalised these columns held a full URL, and rendering the id
    straight into an href produced plain text instead of a working link. `abs/<id>`
    resolves to the latest version.
    """
    aid = (paper.get("arxiv_id") or "").strip()
    return f"https://arxiv.org/abs/{aid}" if aid else ""


def record_reported(conn, papers, report_date: str) -> int:
    """Mark papers as reported; returns the number of DISTINCT papers written.

    A paper listed twice in one briefing (it can appear in two sections) counts
    once. A ledger failure must never lose the report, so each write is guarded.
    """
    ensure_ledger(conn)
    distinct = []
    seen = set()
    for p in papers:
        aid = (p.get("arxiv_id") or "").strip()
        if aid and aid not in seen:
            seen.add(aid)
            distinct.append(aid)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    written = 0
    for aid in distinct:
        try:
            conn.execute(
                f"""INSERT INTO {LEDGER_TABLE}
                        (arxiv_id, first_reported_at, last_report_date, times_reported)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(arxiv_id) DO UPDATE SET
                        last_report_date = excluded.last_report_date,
                        times_reported   = CASE
                            WHEN last_report_date = excluded.last_report_date
                            THEN times_reported
                            ELSE times_reported + 1 END""",
                (aid, now, report_date))
            written += 1
        except sqlite3.Error:
            continue
    conn.commit()
    return written


def get_last_report_cutoff():
    """Cutoff timestamp for this briefing: rolling start-of-day, 7 days back.

    "Since last report" is NOT meant literally. This is a weekly job, so the
    window is always the past REPORT_WINDOW_DAYS days counted from midnight —
    that is what captures everything new during the week.

    Deliberately does NOT read the .last_report marker. That marker is stamped at
    the END of a run, so using it as the cutoff collapses the window to seconds:
    the report then says "no new papers" even though a full week's worth were
    just pulled, and a manual re-run cannot backfill a missed briefing. The
    marker is still written for other tooling, but it never sets the window.
    """
    cutoff_day = (datetime.datetime.now() - datetime.timedelta(days=REPORT_WINDOW_DAYS)).date()
    return datetime.datetime.combine(cutoff_day, datetime.time.min).isoformat()

# ── 7-Category Relevance Scoring Engine (from INTERESTS.md) ──────────────────

# Tier 1 keywords (Categories 1-3: Crime Analysis, Criminal Justice Reform, Legal AI) → +5 each
TIER1_KEYWORDS = [
    # Crime Analysis & Policing Science
    "predictive policing", "crime forecasting", "hotspot analysis", "proactive policing",
    "intelligence-led policing", "intelligence analysis", "crime pattern analysis",
    "strategic analysis", "criminal network analysis", "gang networks", "organized crime",
    "social network analysis", "hate crime patterns", "bias crime", "hate crime analysis",
    "hate crime data", "police data analysis", "data-driven policing", "evidence-based policing",
    "crime", "policing", "police", "criminal", "sentencing", "recidivism",
    "bail", "risk assessment", "forensic", "court", "judicial",
    "hate crime", "law enforcement", "justice",
    # Criminal Justice Reform
    "bail decisions", "bail risk assessment", "pretrial detention", "bail reform",
    "sentencing algorithms", "sentencing guidelines", "sentencing disparity",
    "recidivism prediction", "recidivism risk assessment", "reoffending",
    "risk assessment tool", "risk assessment tools", "actuarial risk", "structured decision-making",
    # Legal AI & Court Technology
    "legal nlp", "legal language model", "legal language models", "legal reasoning",
    "statutory interpretation", "judgment prediction", "court outcome prediction",
    "case outcome forecasting", "court automation", "e-court", "digital justice",
    "case management systems", "ai in legal", "legal ai", "machine learning for law",
    "computational law",
]

# Tier 2 keywords (Categories 4-5: AI Ethics/Fairness, Restorative Justice/JP) → +3 each
TIER2_KEYWORDS = [
    # AI Ethics & Fairness in Justice
    "algorithmic bias", "algorithmic fairness", "fairness in ml", "bias in criminal justice",
    "explainable ai", "xai", "interpretable ml", "transparent algorithms",
    "fairness metrics", "fairness constraints", "disparate impact", "demographic parity",
    "human-ai collaboration", "human-in-the-loop", "decision support system",
    "decision support systems",
    "algorithmic fairness", "bias", "fairness", "explainable", "interpretability",
    "ethics", "nlp legal",
    # Restorative Justice & Justice of the Peace
    "restorative justice", "restorative practices", "community justice",
    "diversion programs", "alternative sentencing", "problem-solving courts",
    "judicial decision-making", "judicial discretion", "judicial reasoning",
    "procedural fairness", "natural justice", "fair hearing",
    "justice of the peace", "bail hearings", "peace officer", "summary conviction",
]

# Tier 3 keywords (Category 6: Diabetes/Health) → +2 each
TIER3_KEYWORDS = [
    "continuous glucose monitoring", "cgm", "glucose sensors", "noninvasive glucose",
    "type 2 diabetes interventions", "diabetes management", "lifestyle interventions",
    "machine learning diabetes", "ai glucose prediction", "diabetes data analysis",
    "metabolic health", "insulin resistance", "glycemic variability", "glucose metabolism",
    "stress glucose", "cortisol blood sugar", "sleep diabetes", "exercise glucose",
    "diabetes", "glucose",
]

# Tier 4 keywords (Category 7: Astronomy & Astrophysics) → +2 each
TIER4_KEYWORDS = [
    "exoplanet", "exoplanet atmosphere", "exoplanet detection", "habitable zone",
    "exoplanet characterization",
    "cosmology", "dark matter", "dark energy", "large scale structure", "cosmic evolution",
    "black hole", "black hole formation", "supermassive black hole", "gravitational wave",
    "ligo", "neutron star", "compact object",
    "galaxy formation", "galaxy evolution", "stellar evolution", "star formation",
    "supernova", "astrophysics", "astronomy",
    "solar system", "planetary science", "asteroid", "astrobiology",
    "astronomical survey", "space telescope", "james webb", "jwst",
    "astronomical instrumentation",
]

# Method-match keywords → +1 each
METHOD_KEYWORDS = [
    "machine learning", "deep learning", "neural network", "neural networks",
    "nlp", "natural language", "transformer", "large language model",
    "llm", "reinforcement learning", "supervised learning", "unsupervised learning",
    "deep reinforcement learning",
]

# Category tags for each keyword group
TIER1_TAG = "crime-analysis,criminal-justice-reform,legal-ai"
TIER2_TAG = "ai-ethics-fairness,restorative-jp"
TIER3_TAG = "diabetes-health"
TIER4_TAG = "astronomy"
METHOD_TAG = "method-match"


def discover_arxiv_id(arxiv_id_field):
    """Extract the short Arxiv ID from any format. Returns None if not an arXiv ID."""
    m = re.search(r'(?:arxiv\.org/abs/)?(\d{4}\.\d{4,5})', str(arxiv_id_field))
    return m.group(1) if m else None


def fetch_semantic_scholar(arxiv_id):
    """Fetch citation data from Semantic Scholar API for a paper."""
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}?fields=citationCount,influentialCitationCount"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return {
            "citation_count": data.get("citationCount", 0) or 0,
            "influential_citation_count": data.get("influentialCitationCount", 0) or 0,
        }
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"  [S2] Rate limited (429). Will retry next run.")
        else:
            print(f"  [S2] HTTP {e.code} for {arxiv_id}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        print(f"  [S2] Error for {arxiv_id}: {e}")
        return None


def fetch_openalex(arxiv_id):
    """Fetch citation count from OpenAlex (free, no rate limit).
    Uses DOI format: 10.48550/arXiv.{arxiv_id}
    """
    url = f"https://api.openalex.org/works/doi:10.48550/arXiv.{arxiv_id}?select=cited_by_count&mailto={MAILTO}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        c = data.get("cited_by_count", 0) or 0
        return {"citation_count": c, "influential_citation_count": 0}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def enrich_citations(conn, batch_limit=200):
    """Enrich papers with missing citation data.

    Uses OpenAlex (free, no rate limit) as primary source. Falls back to
    Semantic Scholar for influential citation counts. Only processes
    up to *batch_limit* papers per run, prioritized by relevance_score.
    """
    c = conn.cursor()
    c.execute("SELECT id, arxiv_id, citation_count FROM papers WHERE (citation_count IS NULL OR citation_count = 0) AND relevance_score > 0 ORDER BY relevance_score DESC LIMIT ?", (batch_limit,))
    papers_to_enrich = [{"id": r[0], "arxiv_id": r[1], "citation_count": r[2]} for r in c.fetchall()]
    
    if not papers_to_enrich:
        print("  [S2] All papers already have citation data. Skipping enrichment.")
        return
    
    print(f"  [S2] Enriching {len(papers_to_enrich)} papers via OpenAlex...")
    enriched_count = 0
    
    for i, paper in enumerate(papers_to_enrich):
        arxiv_id = discover_arxiv_id(paper["arxiv_id"])
        if not arxiv_id:
            c.execute("UPDATE papers SET citation_count = 0 WHERE id = ?", (paper["id"],))
            continue
        
        # OpenAlex primary (no rate limit — can fire fast)
        result = fetch_openalex(arxiv_id)
        
        if result:
            c.execute(
                "UPDATE papers SET citation_count = ? WHERE id = ?",
                (result["citation_count"], paper["id"])
            )
            enriched_count += 1
        else:
            # Mark as 0 so we don't retry (not in OpenAlex index)
            c.execute("UPDATE papers SET citation_count = 0 WHERE id = ?", (paper["id"],))
    
    conn.commit()
    
    # Check remaining
    c.execute("SELECT COUNT(*) FROM papers WHERE citation_count IS NULL OR citation_count = 0")
    remaining = c.fetchone()[0]
    print(f"  [S2] Enriched {enriched_count}/{len(papers_to_enrich)} this run. {remaining} still uncached.")


def score_paper(title, summary, citation_count=0):
    """Score a paper using the 7-category relevance engine.

    Design goals (rebalanced Aug 2026):
    - Keyword contributions are CAPPED per tier, so a paper that sprays
      keywords across many categories can't rack up 30+ points. A genuinely
      deep paper on one topic now competes with a kitchen-sink paper.
    - DEPTH bonus: papers matching exactly one interest category get +3,
      two categories +1. Rewards focused, on-topic work.
    - CITATION bonus is log-scaled (min(10, log2(1+cites))): a 300-cite
      influential paper gets +8, a 1000-cite gets +10. Influence can now
      outweigh keyword breadth — the whole point of the influential channel.
    """
    text = f"{title} {summary}".lower()
    score = 0
    reasons = []
    category_tags = []
    cats_matched = 0

    # Tier 1 (Categories 1-3): +5 per keyword, capped at +8
    t1_hits = [kw for kw in TIER1_KEYWORDS if kw in text]
    if t1_hits:
        score += min(8, 5 * len(t1_hits))
        reasons.append(f"tier1:{len(t1_hits)}kw")
        category_tags.append(TIER1_TAG)
        cats_matched += 1

    # Tier 2 (Categories 4-5): +3 per keyword, capped at +4
    t2_hits = [kw for kw in TIER2_KEYWORDS if kw in text]
    if t2_hits:
        score += min(4, 3 * len(t2_hits))
        reasons.append(f"tier2:{len(t2_hits)}kw")
        category_tags.append(TIER2_TAG)
        cats_matched += 1

    # Tier 3 (Category 6): +2 per keyword, capped at +3
    t3_hits = [kw for kw in TIER3_KEYWORDS if kw in text]
    if t3_hits:
        score += min(3, 2 * len(t3_hits))
        reasons.append(f"tier3:{len(t3_hits)}kw")
        category_tags.append(TIER3_TAG)
        cats_matched += 1

    # Tier 4 (Category 7: Astronomy): +2 per keyword, capped at +3
    t4_hits = [kw for kw in TIER4_KEYWORDS if kw in text]
    if t4_hits:
        score += min(3, 2 * len(t4_hits))
        reasons.append(f"tier4:{len(t4_hits)}kw")
        category_tags.append(TIER4_TAG)
        cats_matched += 1

    # Method match: +1 per keyword, capped at +2
    m_hits = [kw for kw in METHOD_KEYWORDS if kw in text]
    if m_hits:
        score += min(2, len(m_hits))
        reasons.append(f"method:{len(m_hits)}kw")
        category_tags.append(METHOD_TAG)

    # Depth bonus: focused papers beat broad ones
    if cats_matched == 1:
        score += 3
        reasons.append("depth-bonus(1-category)")
    elif cats_matched == 2:
        score += 1
        reasons.append("depth-bonus(2-categories)")

    # Citation bonus: log-scaled so influence actually matters
    if citation_count and citation_count > 0:
        cit_bonus = min(10, int(round(math.log2(1 + citation_count))))
        score += cit_bonus
        reasons.append(f"citation-bonus({citation_count})")

    # Deduplicate category tags
    unique_tags = sorted(set(",".join(category_tags).split(",")))
    categories_str = ",".join(t for t in unique_tags if t)

    return score, reasons, categories_str


def generate_report():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # ── Step 1: Determine cutoff — only report on papers since last briefing ──
    cutoff = get_last_report_cutoff()
    print(f"Reporting cutoff: {cutoff}")
    
    # ── Step 2: Enrich citation data from Semantic Scholar ──
    print("Step 1: Enriching citation data from Semantic Scholar...")
    enrich_citations(conn)
    
    # ── Step 3: Get NEW papers (since last report) for scoring ──
    print("Step 2: Scoring new papers with 7-category relevance engine...")
    # Report once, never again: anything already reported on a PREVIOUS day is
    # excluded from every section below, including the static top-cited list.
    # Today's own reports are exempt so that re-running the generator on the same
    # day reproduces the same briefing instead of emptying it.
    reported_ids = load_reported(conn, before=datetime.datetime.now().strftime("%Y-%m-%d"))
    c.execute("SELECT * FROM papers WHERE ingested_at >= ? ORDER BY published DESC, ingested_at DESC", (cutoff,))
    new_papers = [dict(r) for r in c.fetchall()]
    new_papers = [p for p in new_papers if (p.get("arxiv_id") or "") not in reported_ids]
    if reported_ids:
        print(f"  ledger: {len(reported_ids)} paper(s) already reported are suppressed")
    
    # Get full DB stats for context
    c.execute("SELECT MAX(ingested_at) FROM papers")
    last_update = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM papers")
    total_count = c.fetchone()[0]
    
    c.execute("SELECT COUNT(DISTINCT search_query) FROM papers")
    query_count = c.fetchone()[0]
    
    c.execute("SELECT MIN(published), MAX(published) FROM papers")
    date_range = c.fetchone()
    
    c.execute("SELECT COUNT(*) FROM papers WHERE ingested_at >= ?", (cutoff,))
    recent_count = c.fetchone()[0]
    # Report what is actually being reported: after the ledger filter, `new_papers`
    # is the set this briefing covers, so the header stat and the narrative agree.
    if reported_ids:
        recent_count = len(new_papers)
    
    # Notable papers NEW to the DB inside the window, by citation count.
    # Deliberately window-scoped. The previous version had NO date filter, so it
    # returned the same all-time leaders every run — the identical five papers, all
    # ingested 2026-08-02, appeared in the 21 Sep / 28 Sep / 5 Oct briefings. An
    # all-time citation ranking is a DB statistic, not briefing content, and once
    # the ledger suppresses what has been reported its remainder is a tail of
    # 7-citation papers: it would still read as a "top 5" while meaning nothing.
    # Window-scoped, it can only show genuinely notable recent arrivals, and it is
    # simply absent when there are none.
    c.execute(
        """SELECT title, arxiv_id, citation_count FROM papers
           WHERE citation_count > 0 AND ingested_at >= ?
           ORDER BY citation_count DESC LIMIT 60""", (cutoff,))
    top_cited = [dict(r) for r in c.fetchall()]
    top_cited = [t for t in top_cited if t["arxiv_id"] not in reported_ids][:5]

    # New influential-channel papers since cutoff (for the High-Impact section)
    c.execute(
        "SELECT * FROM papers WHERE ingested_at >= ? AND source = 'influential' ORDER BY citation_count DESC LIMIT 10",
        (cutoff,),
    )
    influential_new = [dict(r) for r in c.fetchall()]
    influential_new = [p for p in influential_new if (p.get("arxiv_id") or "") not in reported_ids]
    
    # ── Step 4: Score and rank only new papers ──
    scored = []
    for p in new_papers:
        score, reasons, categories = score_paper(
            p["title"], p["summary"], p.get("citation_count", 0) or 0
        )
        scored.append((score, reasons, categories, p))
    
    scored.sort(key=lambda x: -x[0])
    
    # Tier classification based on new thresholds:
    # 8+ → Hot Topics, 4-7 → Worth a Look
    hot_topics = [p for s, _, _, p in scored if s >= 8]
    worth_a_look = [p for s, _, _, p in scored if 4 <= s <= 7]
    
    # ── Step 5: Update per-paper relevance metadata in DB ──
    for s, _, categories, p in scored:
        c.execute(
            "UPDATE papers SET relevance_score = ?, relevance_categories = ? WHERE id = ?",
            (s, categories, p["id"])
        )
    conn.commit()
    
    # ── Step 5: Build full Markdown report ──
    now = datetime.datetime.now().strftime("%B %d, %Y")
    
    # If nothing new, short-circuit
    if not new_papers:
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
        filepath = os.path.join(OUTPUT_DIR, f"arxiv_briefing_{date_str}.md")
        short_filepath = os.path.join(OUTPUT_DIR, f"short_{date_str}.txt")
        no_new = f"📄 **Arxiv Briefing — {now}**\n\nNo new papers since last report.\n"
        with open(filepath, "w") as f:
            f.write(f"# Arxiv Research Briefing — {now}\n\nNo new papers ingested since the last report.\n")
        with open(short_filepath, "w") as f:
            f.write(no_new)
        # Save marker so cutoff advances even on no-paper runs
        with open(LAST_RUN_FILE, "w") as f:
            f.write(datetime.datetime.now().isoformat())
        conn.close()
        print(f"\nReport saved: {filepath}")
        print("No new papers to report.")
        return

    report = f"""# Arxiv Research Briefing — {now}

*Automated briefing tailored to your work: Crime Analysis, Criminal Justice Reform, Legal AI, AI Ethics, Restorative Justice, Health & Astronomy*

---

## Stats at a Glance
- **Total papers in DB:** {total_count}
- **Search topics tracked:** {query_count}
- **Date range:** {date_range[0]} to {date_range[1]}
- **Last update:** {last_update[:19] if last_update else "N/A"}
- **New since last report:** {recent_count}

This briefing covers the {len(new_papers)} papers ingested since the last report."""

    # Top-cited papers section
    if top_cited:
        report += "\n### 🏆 Top-Cited Papers\n"
        for i, tp in enumerate(top_cited, 1):
            report += f"  {i}. **{tp['title']}** — {tp['citation_count']} citations\n"
    
    # High-Impact section: influential-channel papers new since last report
    if influential_new:
        report += "\n### ⭐ High-Impact Papers (Most-Cited in Your Topics)\n"
        for p in influential_new:
            s, _, cats = score_paper(p["title"], p["summary"], p.get("citation_count", 0) or 0)
            published = p["published"][:10]
            summary_short = p["summary"][:250] + ("..." if len(p["summary"]) > 250 else "")
            citations = p.get("citation_count", 0) or 0
            report += f"""**{p['title']}**
**Published:** {published} | **Citations:** {citations} | **Score:** {s} | **Tags:** `{cats}`
**Arxiv:** [{p['arxiv_id']}]({arxiv_url(p)}) | **Topic:** {p['search_query']}

{summary_short}

---

"""
    
    report += "\n---\n\n"
    
    if hot_topics:
        report += "## 🔥 Hot Topics (Score 8+)\n\n"
        for p in hot_topics:
            s, _, cats = score_paper(p["title"], p["summary"], p.get("citation_count", 0) or 0)
            published = p["published"][:10]
            summary_short = p["summary"][:300] + ("..." if len(p["summary"]) > 300 else "")
            citations = p.get("citation_count", 0) or 0
            report += f"""### {p['title']}
**Published:** {published} | **Score:** {s} | **Citations:** {citations} | **Tags:** `{cats}`
**Arxiv:** [{p['arxiv_id']}]({arxiv_url(p)}) | **Categories:** {p['categories']}

{summary_short}

---

"""
    
    if worth_a_look:
        report += "\n## 👀 Worth a Look (Score 4–7)\n\n"
        for p in worth_a_look:
            s, _, cats = score_paper(p["title"], p["summary"], p.get("citation_count", 0) or 0)
            published = p["published"][:10]
            summary_short = p["summary"][:200] + ("..." if len(p["summary"]) > 200 else "")
            citations = p.get("citation_count", 0) or 0
            report += f"""### {p['title']}
**Published:** {published} | **Score:** {s} | **Citations:** {citations} | **Tags:** `{cats}`
**Arxiv:** [{p['arxiv_id']}]({arxiv_url(p)}) | **Categories:** {p['categories']}

{summary_short}

---

"""
    
    if not hot_topics and not worth_a_look:
        report += "\nNo highly relevant papers found in this batch.\n"
    
    # Recently added (other topics)
    scored_ids = set(p["arxiv_id"] for p in hot_topics + worth_a_look)
    new_others = [p for p in new_papers if p["arxiv_id"] not in scored_ids]
    if new_others:
        report += "\n## 📥 Recently Added (Other Topics)\n\n"
        for p in new_others[:5]:
            report += f"""- **{p['title']}** — {p['published'][:10]} — *{p['categories']}* — [{p['arxiv_id']}]({arxiv_url(p)})\n"""
    
    # Save full report
    date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    filename = f"arxiv_briefing_{date_str}.md"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w") as f:
        f.write(report)
    
    # ── Step 6: Build short Telegram version ──
    # Top 5 papers with title + score + relevance tags
    top5_all = scored[:5]
    
    short_report = f"📄 **Arxiv Briefing — {now}**\n\n"
    
    if hot_topics:
        short_report += f"🔥 **{len(hot_topics)} Hot Topics**\n"
        for p in hot_topics[:5]:
            s, _, cats = score_paper(p["title"], p["summary"], p.get("citation_count", 0) or 0)
            short_report += f"• [{s}] {p['title'][:70]}... \\#{cats.replace(',', ' #')}\n"
        short_report += "\n"
    
    if worth_a_look:
        short_report += f"👀 **{len(worth_a_look)} Worth a Look**\n"
        for p in worth_a_look[:3]:
            s, _, cats = score_paper(p["title"], p["summary"], p.get("citation_count", 0) or 0)
            tag_str = cats.replace(",", " #")
            short_report += f"• [{s}] {p['title'][:70]}... \\#{tag_str}\n"
        short_report += "\n"
    
    # High-impact picks from the influential channel
    if influential_new:
        short_report += "⭐ **High-Impact Picks**\n"
        for p in influential_new[:3]:
            cites = p.get("citation_count", 0) or 0
            short_report += f"• [{cites} cites] {p['title'][:70]}...\n"
        short_report += "\n"
    
    # Top 5 overall
    short_report += "**🏆 Top 5 Papers This Briefing**\n"
    for i, (s, _, cats, p) in enumerate(top5_all, 1):
        short_report += f"{i}. [{s}] {p['title'][:60]}... \\#{cats.replace(',', ' #')}\n"
    
    short_report += f"\n📥 {recent_count} new | 📚 {total_count} total | 🔎 {query_count} topics"
    
    short_filepath = os.path.join(OUTPUT_DIR, f"short_{date_str}.txt")
    with open(short_filepath, "w") as f:
        f.write(short_report)

    # ── Ledger: everything this briefing DISPLAYED is now reported and must not
    #    appear again. Only displayed papers are marked — a low-scoring paper that
    #    was ingested but never surfaced stays eligible to surface in a later run.
    #    `new_others[:5]` is the "Recently Added (Other Topics)" list, which can
    #    surface papers in no other section.
    displayed = (list(top_cited) + list(influential_new)
                 + list(hot_topics) + list(worth_a_look) + list(new_others[:5]))
    marked = record_reported(conn, displayed, date_str)
    print(f"Ledger: marked {marked} paper(s) as reported (they will not repeat)")

    conn.close()
    
    # Save marker for next run's cutoff
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.datetime.now().isoformat())
    
    print(f"\nReport saved: {filepath}")
    print(f"Short version: {short_filepath}")
    print(f"\nSummary: {len(hot_topics)} hot topics (8+), {len(worth_a_look)} worth a look (4-7), {recent_count} new")
    print(f"Top-cited paper: {top_cited[0]['title'][:60] if top_cited else 'N/A'} ({top_cited[0]['citation_count']} citations)" if top_cited else "")


if __name__ == "__main__":
    generate_report()
