#!/usr/bin/env python3
"""
Bi-weekly interest evaluation for the arxiv query set.   (v2 — 2026-10-01)

WHAT IT DOES
  Mines the user's own Obsidian daily notes (and, as a second signal, the
  papers already ingested) for recurring phrases, then asks TypeSafe Jev
  whether each phrase is a genuine NEW interest area worth a query.

WHY v2 EXISTS — the v1 tool produced ZERO suggestions for four months
  v1: last suggestion 2026-07-01, queries.json untouched since 2026-07-02,
  eight consecutive runs reporting "No new suggestions" while both sources
  were supplying healthy keyword lists. Three defects, all fixed here:

  1. mtime IS NOT A DATE on this vault. The vault lives on a Google Drive
     FUSE mount, which rewrites mtimes on sync. Measured 2026-10-01: of the
     116 files v1 treated as "modified in the last 60 days", 59 were actually
     older than 60 days -- the window leaked ~5 months of history while
     claiming to be 60 days. v2 dates every file from its FILENAME
     (raw/daily/YYYY-MM-DD.md) and ignores files with no parseable date.
     mtime is never consulted.

  2. v1's gates were closed by construction. It rejected every single-word
     candidate unconditionally, and rejected any phrase whose words were all
     already present anywhere in queries.json. With 28 existing queries the
     vocabulary was saturated, so nothing could ever pass. v1 also required a
     hard-coded DOMAIN_TERMS hit -- a list that restates the CURRENT
     interests, which means the tool could only ever rediscover what it
     already knew. v2 keeps DOMAIN_TERMS purely as a SCORING bonus and moves
     the actual judgement to Jev, which can recognise a topic that is not
     already on the list.

  3. Half of v1's input was the arxiv DB -- papers fetched BY the queries the
     tool exists to extend. That is a confirmation loop, and in v2's first
     smoke test it dominated the ranking outright: the top candidates were
     'learning', 'models', 'detection' -- generic paper boilerplate, not
     anything the operator wrote. Candidates come from their OWN vault only:
     raw/ digests + the curated concepts/ and entities/ wiki pages (windowed
     by frontmatter `updated:`). --include-db opts the paper corpus in as a
     secondary source; interests come from what he keeps, not what the
     pipeline already fetched.

CALL SHAPE (one round trip per candidate, three atomic questions)
  coherent      : noul -- is this one coherent nameable topic, or word salad?
  is_interest   : noul -- is it a genuine subject area, not a tool/place/one-off?
  covered       : noul -- is it ALREADY substantially covered by the existing set?

GATES (all must pass; a failed gate just drops the candidate)
  coherent    >= 0.60
  is_interest >= 0.60
  covered     <  0.45

The three-questions-in-one-call shape is deliberate: Jev's confidence is
calibrated to the choice AMONG OFFERED OPTIONS, not to whether the framing of
the question is right, so a second and third atomic question is what catches a
candidate the first question would have waved through.

SAFETY
  DRY RUN BY DEFAULT. Nothing is written unless --apply is passed, and --apply
  backs up queries.json first. This is a cron job; an unattended job must not
  silently mutate the research pipeline's query set.
  Exit codes: 0 ok (with or without suggestions), 2 no usable source at all.

USAGE
  python3 evaluate_interests.py                     # 30-day window, dry run
  python3 evaluate_interests.py --shadow-days 130   # wide reconstruction, no writes
  python3 evaluate_interests.py --apply             # write accepted suggestions
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.expanduser("~/.hermes/.env")


def env_value(key: str, default: str = "") -> str:
    """Value from the environment, else the same key in ~/.hermes/.env.

    Host-specific paths and keys live in the environment, never in the source, so this
    file can be published without carrying somebody's username, cloud layout or address.
    Same lookup order the API key already used.
    """
    val = os.environ.get(key) or ""
    if not val:
        try:
            with open(ENV_PATH, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(key + "="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    return val or default


QUERIES_PATH = os.path.join(BASE_DIR, "queries.json")
DB_PATH = os.path.join(BASE_DIR, "arxiv_papers.db")
TRACKING_PATH = os.path.join(BASE_DIR, "interest_tracking.json")
INTERESTS_PATH = os.path.join(BASE_DIR, "INTERESTS.md")
# Vault location comes from the environment (ARXIV_OBSIDIAN_RAW / _CURATED). When unset
# it points at a local directory that simply will not exist, so a published copy degrades
# to "no vault candidates" rather than reaching into somebody's cloud drive.
OBSIDIAN_RAW = env_value("ARXIV_OBSIDIAN_RAW", os.path.join(BASE_DIR, "vault", "raw"))
# Curated wiki pages (concepts/ + entities/). These are session-DERIVED but
# hand-CURATED: they name what the operator chose to keep, so their titles/descriptions
# are a denser interest signal than the raw digests. They carry NO date in the
# filename -- the window uses the `updated:` frontmatter field instead
# (present on 82/84 pages, verified 2026-10-01). Pages with no `updated:` are
# skipped, never guessed at.
OBSIDIAN_CURATED = env_value("ARXIV_OBSIDIAN_CURATED", os.path.join(BASE_DIR, "vault"))
CURATED_DIRS = ("concepts", "entities")
SKIP_DIRS = {"gemini"}  # bulk work docs, not personal interests

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"  # OpenRouter only -- Go/Zen carry no systemone

UPDATED_RE = re.compile(r"^updated:\s*(\d{4}-\d{2}-\d{2})", re.M)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

WINDOW_DAYS = 30          # "the past month"
ACCEPT_COHERENT = 0.60
ACCEPT_INTEREST = 0.60
ACCEPT_COVERED_BELOW = 0.45
MAX_CANDIDATES = 20
MAX_NEW_PER_RUN = 3
CONTEXT_CHARS = 150       # per evidence snippet handed to Jev

# ---------------------------------------------------------------------------
# Word lists (as v1, except DOMAIN_TERMS is a SCORING bonus only, never a gate)
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
    "crime", "police", "policing", "criminal", "justice", "legal", "court",
    "judicial", "judge", "sentencing", "bail", "recidivism", "forensic",
    "offender", "victim", "witness", "evidence", "surveillance", "security",
    "threat", "prevention", "intervention", "diversion", "rehabilitation",
    "causal", "investigation", "intelligence", "prediction", "risk",
    "assessment", "fairness", "ethics", "bias", "explainable", "network",
    "detection", "protest", "hate", "classification", "forecasting",
    "algorithmic", "discrimination", "equity", "accountability",
    "transparency", "interpretable", "enforcement", "algorithm", "machine",
    "learning", "decision", "diabetes", "glucose", "cgm", "metabolic",
    "insulin", "t2d", "astronomy", "exoplanet", "cosmology", "galaxy",
    "hole", "gravitational",
}

_STATS = {"calls": 0, "cost": 0.0}
_KEY: str | None = None


# ---------------------------------------------------------------------------
# Jev client
# ---------------------------------------------------------------------------

def api_key() -> str:
    """OPENROUTER_API_KEY from env, else ~/.hermes/.env. Jev is OpenRouter-only."""
    global _KEY
    if _KEY is None:
        _KEY = env_value("OPENROUTER_API_KEY")
    return _KEY


def jev_decide(state: str, questions: dict, timeout: int = 90) -> dict | None:
    """POST one decisions call. Returns the parsed body or None. Never raises."""
    if not api_key():
        return None
    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key()},
        method="POST")
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
            usage = body.get("usage") or {}
            _STATS["calls"] += 1
            _STATS["cost"] += usage.get("cost") or 0.0
            return body
        except Exception:
            if attempt == 1:
                time.sleep(1.5)
    return None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def dated_notes(days_back: int) -> list[tuple[str, str]]:
    """
    (label, text) for every in-window note from BOTH obsidian sources.

    Source 1 — raw/ digests: FILENAME carries the date (raw/daily/YYYY-MM-DD.md).
    mtime is deliberately ignored: this vault is on a Google Drive FUSE mount
    that rewrites mtimes on sync (measured: 59 of 116 "recent" files were not).
    A file with no date in its name is skipped rather than guessed at.

    Source 2 — curated wiki (concepts/ + entities/): no date in the filename,
    so the window uses the `updated:` frontmatter field (82/84 pages carry it,
    verified 2026-10-01). Pages without `updated:` are skipped, never guessed.

    One page per label, so a raw digest and its curated page both count —
    repetition across sources is what makes a candidate rank.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=days_back)
    out: list[tuple[str, str]] = []

    if os.path.isdir(OBSIDIAN_RAW):
        for root, dirs, files in os.walk(OBSIDIAN_RAW):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for filename in files:
                if not filename.endswith(".md"):
                    continue
                m = DATE_RE.search(filename)
                if not m:
                    continue
                try:
                    fdate = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if fdate < cutoff:
                    continue
                try:
                    with open(os.path.join(root, filename), encoding="utf-8",
                              errors="replace") as fh:
                        out.append((filename, fh.read()))
                except OSError:
                    continue

    for sub in CURATED_DIRS:
        d = os.path.join(OBSIDIAN_CURATED, sub)
        if not os.path.isdir(d):
            continue
        for filename in sorted(os.listdir(d)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(d, filename)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            m = UPDATED_RE.search(text)
            if not m:
                continue
            try:
                udate = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if udate < cutoff:
                continue
            out.append((f"{sub}/{filename}", text))
    return out


def arxiv_text(days_back: int) -> tuple[str, int]:
    """Titles+summaries of papers ingested in the window. Second signal only."""
    if not os.path.exists(DB_PATH):
        return "", 0
    cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            rows = con.execute(
                "SELECT title, summary FROM papers WHERE ingested_at >= ?",
                (cutoff,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return "", 0
    return " ".join(f"{t} {s}" for t, s in rows), len(rows)


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------

def tokenize(text: str, min_length: int = 4) -> list[str]:
    words = re.findall(r"[a-z]+[-'a-z]*[a-z]+", text.lower())
    return [w for w in words
            if len(w) >= min_length and w not in STOPWORDS and w not in GENERIC_NOISE]


def candidates(text: str, min_length: int = 4, top_n: int = MAX_CANDIDATES) -> list[str]:
    """
    Rank unigrams and repeated bigrams. v2 keeps BOTH: refusing single words
    (v1's rule) is what made a genuinely new one-word interest impossible.

    Bigrams keep a '+' between parts so the original boundary survives; callers
    display them with spaces.
    """
    words = tokenize(text, min_length=min_length)
    scores: dict[str, int] = {}
    for w in words:
        scores[w] = scores.get(w, 0) + (3 if w in DOMAIN_TERMS else 1)
    bigrams = [f"{words[i]}+{words[i + 1]}" for i in range(len(words) - 1)]
    for bigram, count in collections.Counter(bigrams).most_common(40):
        if count > 2:
            scores[bigram] = scores.get(bigram, 0) + count * 2

    ranked: list[str] = []
    seen: set[str] = set()
    for term, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        key = term.replace("+", " ")
        if key in seen or len(key) < 5:
            continue
        seen.add(key)
        if score >= 4:
            ranked.append(term)
        if len(ranked) >= top_n:
            break
    return ranked


def evidence_for(phrase: str, corpus: list[tuple[str, str]]) -> list[str]:
    """Up to 2 short snippets showing the phrase in use. Jev is a literal reader."""
    pattern = re.compile(re.escape(phrase).replace(r"\ ", r"[\s\-]+"), re.I)
    found: list[str] = []
    for _name, text in corpus:
        for m in pattern.finditer(text):
            start = max(0, m.start() - CONTEXT_CHARS // 2)
            snippet = " ".join(text[start:m.end() + CONTEXT_CHARS // 2].split())
            if snippet and snippet not in found:
                found.append(snippet)
            if len(found) >= 2:
                return found
    return found


def existing_interest_labels() -> str:
    """Declared interest areas from INTERESTS.md, compacted for Jev's state."""
    labels: list[str] = []
    try:
        with open(INTERESTS_PATH, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("**Tag:**"):
                    parts = line.split("`")
                    if len(parts) > 1:
                        labels.append(parts[1])
    except OSError:
        pass
    if not labels:
        try:
            labels = [k.replace("_", " ") for k in json.load(open(QUERIES_PATH))]
        except OSError:
            labels = []
    return "; ".join(labels)


# ---------------------------------------------------------------------------
# The Jev gate
# ---------------------------------------------------------------------------

def judge(phrase: str, snippets: list[str], existing: str) -> dict:
    """
    Three atomic noul questions in ONE call.

    Asking all three together is the point: Jev's confidence is calibrated to
    the choice among the offered options, so a single "is this an interest?"
    question will happily approve word salad. `coherent` polices the phrase
    itself and `covered` polices the option set (the existing interests).
    """
    display = phrase.replace("+", " ")
    state = (
        "A recurring phrase was extracted from the user's own daily notes.\n"
        "Phrase: %r\n"
        "Context where it appears: %s\n"
        "The user's EXISTING declared research interests are: %s\n"
        % (display,
           " | ".join(repr(s) for s in snippets) or "(no context available)",
           existing)
    )
    questions = {
        "coherent": {
            "type": "noul",
            "instructions": ("Is this phrase a single coherent, nameable subject or "
                             "topic? Answer no if it is a fragment of a sentence, or "
                             "unrelated words that merely appeared next to each other."),
        },
        "is_interest": {
            "type": "noul",
            "instructions": ("Is this phrase a genuine recurring SUBJECT-MATTER interest "
                             "-- a field, discipline or topic area someone would want "
                             "academic research on? Answer no for tool names, project "
                             "artifacts, product names, place names, people's names, "
                             "sports teams, or a one-off mention."),
        },
        "covered": {
            "type": "noul",
            "instructions": ("Is this phrase's subject matter ALREADY substantially "
                             "covered by the existing declared interests listed above?"),
        },
    }
    body = jev_decide(state, questions)
    if not body:
        return {"ok": False, "reason": "jev_unavailable", "phrase": display}

    a = body.get("answers") or {}

    def num(key):
        v = (a.get(key) or {}).get("noul")
        return v if isinstance(v, (int, float)) else None

    coherent, interest, covered = num("coherent"), num("is_interest"), num("covered")
    if coherent is None or interest is None or covered is None:
        return {"ok": False, "reason": "malformed_answer", "phrase": display}

    base = {"phrase": display, "coherent": coherent,
            "is_interest": interest, "covered": covered}
    if coherent < ACCEPT_COHERENT:
        return {**base, "ok": False, "reason": "not_coherent"}
    if interest < ACCEPT_INTEREST:
        return {**base, "ok": False, "reason": "not_an_interest"}
    if covered >= ACCEPT_COVERED_BELOW:
        return {**base, "ok": False, "reason": "already_covered"}

    parts = display.split()
    return {**base, "ok": True, "reason": "accepted",
            "name": re.sub(r"[^a-z0-9]+", "_", display.lower()).strip("_")[:40],
            "query": "all:" + "+AND+all:".join(parts[:3])}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Suggest new arxiv interest queries.")
    ap.add_argument("--apply", action="store_true",
                    help="write accepted suggestions to queries.json (default: dry run)")
    ap.add_argument("--shadow-days", type=int, default=None,
                    help="reconstruct over this many days; never writes")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES)
    ap.add_argument("--include-db", action="store_true",
                    help="also mine the arxiv DB (off by default: it is a "
                         "confirmation loop -- those papers were fetched by the "
                         "very queries this tool exists to extend)")
    ap.add_argument("--budget-usd", type=float, default=0.25)
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when there is nothing to suggest")
    ap.add_argument("--explain", action="store_true",
                    help="print every candidate with its verdict, not just the rejects")
    args = ap.parse_args()

    window = args.shadow_days or args.window_days
    shadow = args.shadow_days is not None
    now = datetime.datetime.now()

    notes = dated_notes(window)
    db_text, n_papers = (arxiv_text(window) if args.include_db else ("", 0))

    if not notes and not db_text:
        print(f"[error] no usable source: no dated notes in the last {window} days "
              f"under {OBSIDIAN_RAW}, and no papers in {DB_PATH}")
        return 2

    corpus = notes
    journal_text = "\n".join(t for _n, t in notes)
    existing = existing_interest_labels()

    cands = candidates(journal_text + " " + db_text,
                       min_length=4, top_n=args.max_candidates)
    if not cands:
        if not args.quiet:
            print(f"[info] no candidates found in {len(notes)} notes / "
                  f"{n_papers} papers")
        return 0

    queries = json.load(open(QUERIES_PATH))
    accepted: list[dict] = []
    rejected: list[tuple[str, str]] = []

    for raw_term in cands:
        if _STATS["cost"] > args.budget_usd:
            print(f"[warn] budget guard hit (${_STATS['cost']:.4f}) — stopping")
            break
        display = raw_term.replace("+", " ")
        if display in queries:
            continue
        snippets = evidence_for(display, corpus)
        if not snippets:
            rejected.append((display, "no_context"))
            continue
        verdict = judge(raw_term, snippets, existing)
        if verdict.get("ok"):
            accepted.append(verdict)
            if len(accepted) >= MAX_NEW_PER_RUN:
                break
        else:
            rejected.append((display, verdict.get("reason", "?")))

    if args.quiet and not accepted:
        return 0

    tag = "SHADOW (nothing written)" if shadow else ("APPLY" if args.apply else "DRY RUN")
    print(f"=== Interest Evaluation: {now:%Y-%m-%d %H:%M} [{tag}] ===")
    print(f"  sources: {len(notes)} dated notes (last {window}d), "
          f"{n_papers} arxiv papers")
    print(f"  judged: {len(accepted) + len(rejected)} candidates "
          f"(Jev calls {_STATS['calls']}, cost ${_STATS['cost']:.5f})")

    if accepted:
        print("  NEW interests:")
        for s in accepted:
            print(f"    + {s['name']}")
            print(f"        query: {s['query']}")
            print(f"        coherent {s['coherent']:.2f} | interest "
                  f"{s['is_interest']:.2f} | already_covered {s['covered']:.2f}")
        if args.apply and not shadow:
            shutil.copy2(QUERIES_PATH, QUERIES_PATH + ".bak")
            for s in accepted:
                queries[s["name"]] = s["query"]
            with open(QUERIES_PATH, "w") as fh:
                json.dump(queries, fh, indent=2)
                fh.write("\n")
            print(f"  applied -> queries.json now holds {len(queries)} queries "
                  f"(backup: queries.json.bak)")
        else:
            print("  (dry run — re-run with --apply to write these)")
    else:
        print("  no new interests passed the gate")

    if rejected:
        shown = rejected if args.explain else rejected[:12]
        print("  rejected: " + ", ".join(f"{p} ({r})" for p, r in shown))

    if not shadow:
        tracking = {"v": 1, "pk": [], "sg": [], "hi": []}
        if os.path.exists(TRACKING_PATH):
            try:
                tracking = json.load(open(TRACKING_PATH))
            except (OSError, json.JSONDecodeError):
                pass
        tracking.setdefault("hi", []).append({
            "d": str(now)[:10],
            "j": [c.replace("+", " ") for c in cands[:3]],
            "db": [],
            "n": len(accepted),
            "src": "obsidian+db" if db_text else "obsidian",
            "notes": len(notes),
        })
        with open(TRACKING_PATH, "w") as fh:
            json.dump(tracking, fh, indent=2)
            fh.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
