# ArXiv Research Pipeline

Automated academic paper fetching, relevance scoring and weekly briefing generation,
feeding a podcast stream.

## Pipeline

| Module | Job |
|---|---|
| `arxiv/update_arxiv.py` | Pull new papers by query from arXiv |
| `arxiv/update_influential.py` | Churn top-cited papers per topic via OpenAlex citation enrichment |
| `arxiv/generate_report.py` | Build the daily briefing report |
| `arxiv/deep_dive.py` | Weekly deep dive on a single paper (full-text PDF extraction) |
| `arxiv/evaluate_interests.py` | Evaluate interest-category effectiveness (Jev-gated) |
| `arxiv/regenerate_report_checks.py` | Checks for report regeneration |

**Run the arXiv scripts under `python3` (the system default), not a venv.** `pymupdf` /
`fitz` is installed there. One interpreter lost `fitz` after an update and silently skipped
full-text extraction — the run still "succeeded" and quietly produced no text.

## Interest profile

- `arxiv/INTERESTS.md` — the formal interest profile driving relevance scoring: 28 queries
  across 6 categories.
- `arxiv/queries.json` — the query set itself.

## Configuration

Host-specific paths live in the **environment**, never in source, and are read from the
environment or `~/.hermes/.env`:

- `ARXIV_OBSIDIAN_RAW` — vault digests used as an interest signal
- `ARXIV_OBSIDIAN_CURATED` — curated wiki pages (a denser signal; they name what was kept)
- `OPENROUTER_API_KEY` — the evaluator's model calls are OpenRouter-only

Unset, the vault paths point at a local directory that does not exist, so a fresh copy
degrades to "no vault candidates" rather than reaching into someone's cloud drive.

## Runners

- `scripts/run_arxiv_pipeline.sh`
- `scripts/run_arxiv_deep_dive.sh`
- `scripts/run_arxiv_eval.sh`
- `scripts/build_arxiv_db.py`
- `scripts/generate_arxiv_podcast_data.py`

## Cron

| Job | Schedule |
|---|---|
| Pipeline + podcast | Mon 09:00 |
| Deep dive | Sun 07:00 |
| Interest evaluator | 1st and 15th, 06:00 |

## The reporting window — read before touching a date filter

**"Since last report" is never literal.** A weekly job always means the past 7 days, i.e.
midnight of `today - 7`. Do **not** key a cutoff off a `.last_report`-style marker written
at the end of the previous run — the window collapses to seconds and the briefing comes
back empty. Overlap between consecutive briefings is expected and correct.

## Not versioned

The papers DB, PDFs, generated audio, `reports/` and `interest_tracking.json` are runtime
data. `arxiv/podcast/` is gitignored: it holds generated audio plus a one-off TTS helper
carrying a hardcoded date, and podcast generation is owned by the `podcast-pipeline` repo.
