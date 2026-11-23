"""OpenAlex citation lookups — one request per 50 papers.

Both ``generate_report.py`` and ``update_influential.py`` use this module.
Import-safe: no side effects, no network on import.

OpenAlex notes (verified 2026-11-22):
- The ``doi:`` filter takes ONE prefix and pipe-joined values:
  ``filter=doi:10.48550/arXiv.A|10.48550/arXiv.B``. Repeating ``doi:`` per value
  is a 400.
- A work's primary DOI is often the PUBLISHED version (10.1089/...), not the
  arXiv DOI you filtered by, so results are matched by scanning every
  ``locations`` entry for an ``arxiv.org/abs/<id>`` URL as well.
- Without an API key the free daily budget is shared per IP (~$0.10); a free key
  gets its own ~$1/day. Set ``OPENALEX_API_KEY`` in the environment or
  ``~/.hermes/.env``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

BATCH = 50
DEFAULT_MAILTO = "agentvi@agentmail.to"
DEFAULT_UA = f"HermesArxivBriefing/1.0 (mailto:{DEFAULT_MAILTO})"

_KEY_CACHE = None


def api_key() -> str:
    """Optional free key. Environment first, then ~/.hermes/.env."""
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key:
        try:
            with open(os.path.expanduser("~/.hermes/.env"), errors="replace") as fh:
                for line in fh:
                    name, _, value = line.strip().partition("=")
                    if name == "OPENALEX_API_KEY" and value:
                        key = value.strip().strip("\"'")
                        break
        except OSError:
            pass
    _KEY_CACHE = key
    return key


def fetch_batch(arxiv_ids, *, mailto: str = DEFAULT_MAILTO,
                user_agent: str | None = None, timeout: int = 30):
    """``{requested_id: cited_by_count}`` for up to 50 ids, or None on failure.

    A successful request returns an entry for every id OpenAlex knows about;
    ids it does not know are simply absent (treat them as 0 citations). None
    means the request itself failed — do not stamp those papers as checked.
    """
    ids = [i for i in (arxiv_ids or []) if i]
    if not ids:
        return {}
    work_filter = "doi:" + "|".join(f"10.48550/arXiv.{i}" for i in ids[:BATCH])
    url = (f"https://api.openalex.org/works?filter={work_filter}"
           f"&select=doi,cited_by_count,locations&per-page={BATCH}&mailto={mailto}")
    key = api_key()
    if key:
        url += f"&api_key={key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent or DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError):
        return None

    by_lower = {i.lower(): i for i in ids}
    out = {}
    for item in data.get("results", []) or []:
        count = int(item.get("cited_by_count") or 0)
        matched = set()
        urls = [item.get("doi") or ""]
        for loc in item.get("locations") or []:
            urls += [loc.get("doi") or "", loc.get("landing_page_url") or "",
                     loc.get("pdf_url") or ""]
        for raw in urls:
            low = raw.lower()
            if "arxiv." in low:
                requested = by_lower.get(low.rsplit("arxiv.", 1)[-1])
                if requested:
                    matched.add(requested)
            match = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:v\d+)?/?$", low)
            if match:
                requested = by_lower.get(match.group(1))
                if requested:
                    matched.add(requested)
        for requested in matched:
            out[requested] = count
    return out
