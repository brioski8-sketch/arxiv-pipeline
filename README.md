# ArXiv Research Pipeline

Automated academic paper fetching, relevance scoring and weekly briefing generation,
feeding a podcast stream.

## Pipeline

| Module | Job |
|---|---|
| `arxiv/update_arxiv.py` | Pull new papers by query from arXiv |
| `arxiv/update_influential.py` | Churn top-cited papers per topic via OpenAlex citation enrichment |
| `arxiv/openalex_client.py` | Shared OpenAlex lookup — 50 DOIs per request, maps published DOIs back to arXiv ids |
| `arxiv/generate_report.py` | Build the weekly briefing report |
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

- `scripts/run_arxiv_pipeline.sh` — pull + report (Monday)
- `scripts/run_arxiv_influential.sh` — influential/most-cited channel (Tuesday)
- `scripts/run_arxiv_deep_dive.sh`
- `scripts/run_arxiv_eval.sh`
- `scripts/build_arxiv_db.py`
- `scripts/generate_arxiv_podcast_data.py`

## Cron

| Job | Schedule |
|---|---|
| Pipeline + podcast | Mon 09:00 |
| Influential channel | Tue 08:00 |
| Deep dive | Sun 07:00 |
| Interest evaluator | 1st and 15th, 06:00 |

**The influential channel runs on its own day on purpose.** arXiv's rate limit is per-IP and
collective: starting ten relevance queries seconds after the 28-query pull put every request on
an exhausted budget, so it 429'd, never finished, and retried the same work weekly for months.
A separate Tuesday run gets a clean budget. The script deliberately does not stamp its marker on
a partial enrichment, so check `.last_influential` before assuming the channel is alive.

The two agent-facing jobs read their instructions from host-local files (`~/.hermes/CronPrompts/`).
They are deliberately not published: they carry the local delivery paths (Drive folders).

## Logs

Every step writes to `~/.hermes/logs/arxiv/` (`pull_<date>.log`, `report_<date>.log`,
`influential_<date>.log`, `interest_eval.log`). They used to live in `/tmp` and a pruned
scratch dir, which is how a three-month failure stayed invisible.

## The reporting window — read before touching a date filter

**"Since last report" is never literal.** A weekly job always means the past 7 days, i.e.
midnight of `today - 7`. Do **not** key a cutoff off a `.last_report`-style marker written
at the end of the previous run — the window collapses to seconds and the briefing comes
back empty. Overlap between consecutive briefings is expected and correct.

## Not versioned

The papers DB, PDFs, generated audio, `reports/` and `interest_tracking.json` are runtime
data. `arxiv/podcast/` is gitignored: it holds generated audio plus a one-off TTS helper
carrying a hardcoded date, and podcast generation is owned by the `podcast-pipeline` repo.
