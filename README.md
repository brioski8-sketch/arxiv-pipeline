# ArXiv Research Pipeline

Automated academic paper fetching + relevance scoring across interest categories, with podcast generation.

- `arxiv/update_arxiv.py` — pull new papers by query from ArXiv.
- `arxiv/update_influential.py` — churn top-cited papers per topic via OpenAlex citation enrichment.
- `arxiv/generate_report.py` — build the daily briefing report.
- `arxiv/deep_dive.py` — weekly deep dive on a single paper (full-text PDF extraction).
- `arxiv/evaluate_interests.py` — monthly evaluation of interest-category effectiveness.

Feeds the podcast stream via TTS. DB / PDFs / audio are gitignored.
