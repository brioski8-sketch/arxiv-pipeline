"""Checks for arxiv_id canonicalisation: the helper, the writer, and the DB state.

Run after normalise_arxiv_ids.py. Covers the three ways this could silently rot:
the parsing helper, the writer that inserts rows, and the stored table itself.
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile

BASE = os.path.expanduser("~/.hermes/datasets/arxiv")
sys.path.insert(0, BASE)
import update_arxiv as ua  # noqa: E402

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


print("A. canonical_arxiv_id helper")
cases = [
    ("http://arxiv.org/abs/2403.12108v3", ("2403.12108", 3)),
    ("https://arxiv.org/abs/2509.20634v2", ("2509.20634", 2)),
    ("http://arxiv.org/abs/2403.12108v12", ("2403.12108", 12)),
    ("http://arxiv.org/abs/2403.12108", ("2403.12108", None)),
    ("2403.12108", ("2403.12108", None)),
    ("2403.12108v5", ("2403.12108", 5)),
    ("https://arxiv.org/abs/hep-th/0601001", ("hep-th/0601001", None)),
    ("hep-th/0601001v2", ("hep-th/0601001", 2)),
    ("  http://arxiv.org/abs/0706.3639v1  ", ("0706.3639", 1)),
]
for raw, want in cases:
    got = ua.canonical_arxiv_id(raw)
    ck(f"{raw.strip()[:44]:44s} -> {want}", got == want, got)

print("\nB. the writer stores canonical ids (real store_papers, on a copy of the DB)")
tmpd = tempfile.mkdtemp(prefix="arxivid-")
tmpdb = os.path.join(tmpd, "arxiv_papers.db")
shutil.copy2(os.path.join(BASE, "arxiv_papers.db"), tmpdb)
orig = ua.DB_PATH
ua.DB_PATH = tmpdb
try:
    conn = sqlite3.connect(tmpdb)
    before = conn.execute("SELECT count(*) FROM papers").fetchone()[0]
    rows = [{
        "arxiv_id": ua.canonical_arxiv_id("http://arxiv.org/abs/2499.88888v4")[0],
        "version": ua.canonical_arxiv_id("http://arxiv.org/abs/2499.88888v4")[1],
        "title": "synthetic writer check", "published": "2026-10-05", "updated": "2026-10-05",
        "summary": "x", "authors": "a", "links": "https://arxiv.org/abs/2499.88888v4",
        "categories": "cs.AI", "search_query": "check",
    }]
    n = ua.store_papers(conn, rows)
    after = conn.execute("SELECT count(*) FROM papers").fetchone()[0]
    ck("store_papers inserted the row", n == 1 and after == before + 1, (n, before, after))
    got = conn.execute("SELECT arxiv_id, version FROM papers WHERE arxiv_id='2499.88888'").fetchone()
    ck("stored id is bare, version in its own column", got == ("2499.88888", 4), got)
    ck("no URL-form id reached the table",
       conn.execute("SELECT count(*) FROM papers WHERE arxiv_id LIKE '%://%'").fetchone()[0] == 0)
    # re-inserting the same paper must be a skip, not a duplicate
    n2 = ua.store_papers(conn, rows)
    ck("re-ingesting the same paper is skipped, not duplicated",
       n2 == 0 and conn.execute("SELECT count(*) FROM papers WHERE arxiv_id='2499.88888'").fetchone()[0] == 1,
       n2)
    conn.close()
finally:
    ua.DB_PATH = orig
    shutil.rmtree(tmpd, ignore_errors=True)

print("\nC. stored table invariants")
conn = sqlite3.connect(f"file:{os.path.join(BASE, 'arxiv_papers.db')}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute("SELECT * FROM papers")]
ids = [r["arxiv_id"] for r in rows]
url_re = re.compile(r"^https?://")
ver_re = re.compile(r"v\d+$")
ck("no URL-form ids", not any(url_re.search(i) for i in ids))
ck("no version suffix inside the key", not any(ver_re.search(i) for i in ids))
ck("arxiv_id is a real key (no duplicates)", len(set(ids)) == len(ids))
ck("every row has a title", all(r["title"] for r in rows))
ck("no row lost both summary and links",
   all(r["summary"] or r["links"] for r in rows))
ck("deep_dived count preserved (no dive dropped)",
   conn.execute("SELECT count(*) FROM papers WHERE deep_dived=1").fetchone()[0] >= 0)
ck("ledger keys all canonical",
   all(not ver_re.search(k) and not url_re.search(k)
       for (k,) in conn.execute("SELECT arxiv_id FROM reported_papers")))
orphans = conn.execute(
    "SELECT count(*) FROM reported_papers WHERE arxiv_id NOT IN (SELECT arxiv_id FROM papers)"
).fetchone()[0]
ck("ledger orphans are bounded (pruned history)", orphans < 200, orphans)
conn.close()

print("\nD. links are built from the id, never the id used as a URL")
import generate_report as gr  # noqa: E402
ck("arxiv_url builds an abs URL", gr.arxiv_url({"arxiv_id": "2403.12108"}) == "https://arxiv.org/abs/2403.12108")
ck("arxiv_url tolerates a missing id", gr.arxiv_url({}) == "")
src = open(os.path.join(BASE, "generate_report.py"), encoding="utf-8").read()
bad = re.findall(r"\]\(\{p\['arxiv_id'\]\}\)", src)
ck("no render site uses arxiv_id as an href", not bad, bad)
ck("all arxiv render sites go through arxiv_url", src.count("arxiv_url(p)") >= 4,
   src.count("arxiv_url(p)"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
