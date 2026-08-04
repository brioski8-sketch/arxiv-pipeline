#!/usr/bin/env python3
"""
Bi-weekly interest evaluation for arxiv paper queries.

Scans two sources for emerging keywords:
  1. Obsidian JOURNAL vault (.md files modified in last 60 days)
  2. Arxiv papers DB (papers ingested in last 60 days)

Then suggests 1-3 new search queries that don't overlap with existing queries.
Appends new queries to queries.json and tracks history in interest_tracking.json.
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import re
import sqlite3
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUERIES_PATH = os.path.join(BASE_DIR, "queries.json")
DB_PATH = os.path.join(BASE_DIR, "arxiv_papers.db")
TRACKING_PATH = os.path.join(BASE_DIR, "interest_tracking.json")
JOURNAL_VAULT = "/mnt/c/Users/TheBe/OneDrive/Documents/Obsidian Vault/JOURNAL"

# ---------------------------------------------------------------------------
# Word lists
# ---------------------------------------------------------------------------

STOPWORDS: set[str] = {
    "the", "a", "an", "and", "or", "of", "in", "to", "for", "is", "on",
    "that", "this", "with", "as", "by", "at", "from", "be", "are", "was",
    "has", "have", "had", "been", "being", "will", "would", "could", "should",
    "may", "might", "can", "must", "their", "its", "his", "her", "our",
    "your", "my", "we", "they", "it", "he", "she", "not", "no", "but",
    "also", "very", "just", "all", "some", "any", "each", "every", "both",
    "between", "about", "into", "through", "during", "before", "after",
    "above", "below", "over", "under", "such", "more", "most", "other",
    "another", "few", "many", "several", "these", "those", "using", "based",
    "approach", "method", "model", "result", "study", "paper", "work",
    "system", "data", "performance", "task", "problem", "framework",
    "technique", "way", "toward", "while", "still", "yet", "well", "need",
    "however", "although", "due", "across", "within", "without", "large",
    "scale", "high", "low", "set", "show", "shows", "shown", "found",
    "used", "new", "make", "made", "take", "given", "provide", "allows",
    "enables", "aim", "aims", "introduce", "propose", "present", "develop",
    "implement", "apply", "generally", "part", "address", "further",
    "different", "important", "significant", "potential", "existing",
    "current", "previous", "specific", "general",
}

GENERIC_NOISE: set[str] = {
    "grocery", "dashboard", "arxiv", "cron", "todo", "hermes", "session",
    "obsidian", "vault", "journal", "diary", "stats", "monitor", "recipe",
    "finder", "daily", "summary", "lawn", "laundry", "dinner", "walk",
    "park", "banya", "sauna", "steam", "bath", "cooking", "meal", "clean",
    "weekend", "boot", "ground", "shift", "career", "meeting", "report",
    "check", "family", "guy", "kids", "wife", "husband", "son", "daughter",
    "friend", "home", "time", "day", "night", "morning", "afternoon",
    "evening", "week", "month", "year", "thing", "stuff", "people",
    "person", "place", "call", "text", "message", "email", "phone",
    "number", "name", "page", "line", "type", "sort", "kind", "form",
    "part", "point", "level", "class", "group", "area", "section",
    "location", "site", "source", "tool", "working", "boilerplate",
    "complete", "upper", "middle",
}

DOMAIN_TERMS: set[str] = {
    # Crime & justice
    "crime", "police", "policing", "criminal", "justice", "legal", "court",
    "judicial", "judge", "sentencing", "bail", "recidivism", "forensic",
    "offender", "victim", "witness", "evidence", "surveillance", "security",
    "threat", "prevention", "intervention", "diversion", "rehabilitation",
    "causal",
    # Investigation & intelligence
    "investigation", "intelligence", "analysis", "prediction", "risk",
    "assessment", "fairness", "ethics", "bias", "explainable", "network",
    "detection", "protest", "hate", "classification", "forecasting",
    "algorithmic", "bias", "discrimination", "equity", "accountability",
    "transparency", "interpretable", "explainable", "ai", "law",
    "enforcement",
    # ML / AI
    "algorithm", "machine", "learning", "deep", "learning", "decision",
    "support",
    # Diabetes & metabolic
    "diabetes", "glucose", "cgm", "metabolic", "insulin", "t2d",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_queries() -> dict[str, str]:
    """Load existing queries from queries.json."""
    with open(QUERIES_PATH) as fh:
        return json.load(fh)


def save_queries(queries: dict[str, str]) -> None:
    """Persist queries to queries.json."""
    with open(QUERIES_PATH, "w") as fh:
        json.dump(queries, fh, indent=2)
        fh.write("\n")


def load_tracking() -> dict[str, Any]:
    """Load interest tracking history."""
    if os.path.exists(TRACKING_PATH):
        with open(TRACKING_PATH) as fh:
            return json.load(fh)
    return {"v": 1, "pk": [], "sg": [], "hi": []}


def save_tracking(tracking: dict[str, Any]) -> None:
    """Persist tracking history."""
    with open(TRACKING_PATH, "w") as fh:
        json.dump(tracking, fh, indent=2)
        fh.write("\n")


def extract_words(text: str, min_length: int = 4) -> list[str]:
    """Tokenize text into lowercase words, filtering stopwords and noise."""
    words = re.findall(r"[a-z]+[-'a-z]*[a-z]+", text.lower())
    return [
        w for w in words
        if len(w) >= min_length and w not in STOPWORDS and w not in GENERIC_NOISE
    ]


def score_keywords(
    words: list[str],
    domain_bonus: int = 3,
    bigram_bonus_factor: int = 2,
    top_n: int = 15,
) -> list[str]:
    """
    Score extracted words, favouring domain terms and common bigrams.

    Returns a deduplicated, ranked list of keyword strings (bigram tokens
    joined with '+' are kept for downstream matching, then spaces during
    dedup).
    """
    # Unigram scoring
    scores: dict[str, int] = {}
    for w in words:
        bonus = domain_bonus if w in DOMAIN_TERMS else 1
        scores[w] = scores.get(w, 0) + bonus

    # Bigram scoring
    bigrams = [f"{words[i]}+{words[i + 1]}" for i in range(len(words) - 1)]
    for bigram, count in collections.Counter(bigrams).most_common(20):
        if count > 1:
            scores[bigram] = scores.get(bigram, 0) + count * bigram_bonus_factor

    # Rank by score descending
    ranked = sorted(scores.items(), key=lambda x: -x[1])

    # Filter: keep domain-related terms or high-scoring terms
    result: list[str] = []
    seen: set[str] = set()
    for term, score in ranked:
        cleaned = term.replace("+", " ").strip()
        if cleaned in seen:
            continue
        seen.add(cleaned)

        is_domain = any(domain in cleaned or cleaned in domain for domain in DOMAIN_TERMS)
        if is_domain or score > 5:
            # Keep the original concatenated form for ranking, but use cleaned
            # for dedup
            if len(cleaned) >= 4:
                result.append(term)  # keep original token form

        if len(result) >= top_n:
            break

    return result


# ---------------------------------------------------------------------------
# Source scanners
# ---------------------------------------------------------------------------

def scan_journal_vault(days_back: int = 60) -> list[str]:
    """
    Read .md files from the Obsidian JOURNAL vault modified within the last
    *days_back* days, extract and score keywords.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days_back)
    all_text: list[str] = []

    if not os.path.isdir(JOURNAL_VAULT):
        print(f"  [warn] Journal vault not found: {JOURNAL_VAULT}")
        return []

    for filename in os.listdir(JOURNAL_VAULT):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(JOURNAL_VAULT, filename)
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
            if mtime < cutoff:
                continue
            with open(filepath, encoding="utf-8", errors="replace") as fh:
                all_text.append(fh.read())
        except (OSError, IOError) as exc:
            print(f"  [warn] Skipping {filename}: {exc}")

    if not all_text:
        return []

    text = " ".join(all_text)
    words = extract_words(text, min_length=4)
    return score_keywords(words, top_n=15)


def scan_arxiv_db(days_back: int = 60) -> list[str]:
    """
    Query the arxiv papers DB for papers ingested in the last *days_back*
    days, extract and score keywords from titles and summaries.
    """
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days_back)).isoformat()[:10]

    if not os.path.exists(DB_PATH):
        print(f"  [warn] Arxiv db not found: {DB_PATH}")
        return []

    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "SELECT title, summary FROM papers WHERE ingested_at >= ?",
            (cutoff,),
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        print(f"  [error] DB query failed: {exc}")
        return []
    finally:
        con.close()

    if not rows:
        return []

    text = " ".join(f"{title} {summary}" for title, summary in rows)
    words = extract_words(text, min_length=5)  # slightly stricter length
    return score_keywords(words, top_n=10)


# ---------------------------------------------------------------------------
# Query suggestion
# ---------------------------------------------------------------------------

def suggest_queries(
    journal_keywords: list[str],
    db_keywords: list[str],
    existing_queries: dict[str, str],
) -> list[dict[str, str]]:
    """
    From the combined keyword lists, propose 1-3 new Arxiv search queries
    that do not overlap with existing queries in name or term content.

    Each suggestion dict has keys:
      - "name": short snake_case identifier
      - "query": Arxiv search string (all:term1+AND+all:term2 ...)
    """
    existing_names: set[str] = set(existing_queries.keys())
    
    suggestions: list[dict[str, str]] = []
    seen_canonical: set[str] = set()
    # Extract all existing query terms as individual words for subword overlap detection
    all_existing_words: set[str] = set()
    for q in existing_queries.values():
        for raw in re.findall(r"all:([a-z0-9\-+]+)", q.lower()):
            for w in raw.replace("+", " ").split():
                w_clean = w.replace("-", "")
                if len(w_clean) > 3:
                    all_existing_words.add(w_clean)

    combined = journal_keywords + db_keywords

    for kw in combined:
        clean = kw.replace("+", " ").strip().lower()

        # Must be long enough to be meaningful
        if len(clean) < 5:
            continue

        # Must be domain-relevant with reasonable specificity
        # Single word must be specific enough (high score) or part of a multi-word phrase
        clean_words = set(clean.split())
        is_domain = bool(clean_words & DOMAIN_TERMS)
        
        # Get the score from the ranked list (passed through kw token)
        # 'kw' contains the original '+' separated token which carries score info
        # Reject if: single word AND not specifically scored above threshold
        kw_parts = kw.split("+")
        is_single_word = len(kw_parts) == 1
        
        if not is_domain:
            continue
        
        # Reject single-word suggestions — they're too broad for arxiv search
        if is_single_word:
            continue

        # Must not overlap with any existing query name
        if any(clean in en or en in clean for en in existing_names):
            continue

        # Check for word-level overlap with existing queries
        # If every word in the candidate is already covered by existing query words, skip it
        candidate_words = set(clean.split())
        overlapping = candidate_words & all_existing_words
        # If a significant portion of the candidate's words already exist in queries, skip
        if len(overlapping) >= len(candidate_words):
            continue

        # Dedup on canonical form (no spaces)
        canonical = clean.replace(" ", "")
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)

        name = clean.replace(" ", "_").replace("-", "_")[:40]
        parts = clean.split()
        if len(parts) == 1:
            query = f"all:{parts[0]}"
        else:
            query = "all:" + "+AND+all:".join(parts[:3])

        suggestions.append({"name": name, "query": query})
        if len(suggestions) >= 3:
            break

    return suggestions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    now = datetime.datetime.now()
    print(f"=== Interest Evaluation: {now} ===")

    queries = load_queries()
    tracking = load_tracking()

    journal_keywords = scan_journal_vault()
    db_keywords = scan_arxiv_db()

    print(f"  Journal keywords (top 6): {journal_keywords[:6]}")
    print(f"  Arxiv DB keywords (top 6): {db_keywords[:6]}")

    suggestions = suggest_queries(journal_keywords, db_keywords, queries)

    if suggestions:
        print("  New queries to add:")
        for s in suggestions:
            print(f"    + {s['name']}: {s['query']}")
            queries[s["name"]] = s["query"]
        save_queries(queries)
    else:
        print("  No new suggestions.")

    # Record in tracking history
    tracking["hi"].append({
        "d": str(now)[:10],
        "j": journal_keywords[:3],
        "db": db_keywords[:3],
        "n": len(suggestions),
    })
    save_tracking(tracking)

    print(f"  Total queries: {len(queries)}")


if __name__ == "__main__":
    main()
