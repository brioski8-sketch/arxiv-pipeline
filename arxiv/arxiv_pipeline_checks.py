"""Integration checks for the arxiv pipeline (post arxiv_id normalisation).

Exercises the real code the crons run, against COPIES of the DB so the live data is
untouched: the live parser (one real arXiv request), the influential writer, the
deep-dive picker/marker, the report generator, and the wrapper's stdout contract.

Run:  python3 arxiv_pipeline_checks.py
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

BASE = os.path.expanduser("~/.hermes/datasets/arxiv")
sys.path.insert(0, BASE)
import update_arxiv as ua          # noqa: E402
import update_influential as ui    # noqa: E402
import deep_dive as dd             # noqa: E402
import generate_report as gr       # noqa: E402

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def scratch_db():
    d = tempfile.mkdtemp(prefix="arxpipe-")
    p = os.path.join(d, "arxiv_papers.db")
    shutil.copy2(os.path.join(BASE, "arxiv_papers.db"), p)
    return d, p


print("A. the LIVE parser -> store path (one real arXiv request)")
tmpd, tmpdb = scratch_db()
try:
    papers = ua.fetch_arxiv("pipeline_check", "all:recidivism+AND+(all:prediction+OR+all:risk)", max_results=3)
    ck("fetch_arxiv returned papers from the live API", bool(papers), papers)
    if papers:
        ck("every parsed id is already canonical (no URL, no version)",
           all(not re.search(r"://", p["arxiv_id"]) and not re.search(r"v\d+$", p["arxiv_id"])
               for p in papers),
           [p["arxiv_id"] for p in papers][:3])
        ck("every parsed id has a version recorded separately",
           all("version" in p for p in papers))
        conn = sqlite3.connect(tmpdb)
        # These live results are papers the DB already has, so the correct outcome is
        # a SKIP — the UNIQUE key + the pre-insert existence check doing their job.
        n = ua.store_papers(conn, papers)
        ck("store_papers skips live papers already in the DB", n == 0, n)
        # Prove the INSERT path on real parsed data by giving one a fresh canonical id.
        fresh = dict(papers[0], arxiv_id="2499.12345", version=1)
        n1 = ua.store_papers(conn, [fresh])
        ck("store_papers inserts a genuinely new paper", n1 == 1, n1)
        bad = conn.execute("SELECT count(*) FROM papers WHERE arxiv_id LIKE '%://%'").fetchone()[0]
        ck("no URL-form row reached the DB", bad == 0)
        v = conn.execute("SELECT arxiv_id, version FROM papers WHERE arxiv_id='2499.12345'").fetchone()
        ck("stored row has bare id + version", v == ("2499.12345", 1), v)
        # re-running must skip, not duplicate
        n2 = ua.store_papers(conn, [fresh])
        ck("a second pull of the same paper is a no-op", n2 == 0, n2)
        conn.close()
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print("\nB. the influential writer")
tmpd, tmpdb = scratch_db()
try:
    conn = sqlite3.connect(tmpdb)
    row = {"arxiv_id": "2499.77777", "title": "synthetic influential", "published": "2026-10-05",
           "summary": "x", "authors": "a", "query_name": "check", "citation_count": 42}
    added, updated, dupes = ui.store(conn, [row])
    ck("influential store() inserts a bare id", added == 1, (added, updated))
    got = conn.execute("SELECT arxiv_id, citation_count, source FROM papers WHERE arxiv_id='2499.77777'").fetchone()
    ck("row tagged source='influential' with its citation count",
       got == ("2499.77777", 42, "influential"), got)
    # the same paper must be UPDATED, not duplicated, when re-stored
    before = conn.execute("SELECT count(*) FROM papers").fetchone()[0]
    added2, updated2, _ = ui.store(conn, [dict(row, summary="updated text", citation_count=55)])
    after = conn.execute("SELECT count(*) FROM papers").fetchone()[0]
    ck("re-storing the same influential paper does not duplicate it", after == before, (before, after))
    ck("re-store took the UPDATE branch", updated2 == 1 and added2 == 0, (added2, updated2))
    try:
        conn.execute("INSERT INTO papers (arxiv_id,title) VALUES (?,?)",
                     ("http://arxiv.org/abs/2499.66666v1", "T"))
        aborted = False
    except sqlite3.IntegrityError:
        aborted = True
    ck("the trigger still rejects a URL-form write from this path too", aborted)
    conn.close()
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print("\nC. the deep-dive picker and marker")
tmpd, tmpdb = scratch_db()
try:
    dd.DB_PATH = tmpdb
    conn = sqlite3.connect(tmpdb)
    paper = dd.get_top_undived_paper(conn)
    ck("get_top_undived_paper still finds a candidate", bool(paper), paper)
    if paper:
        ck("picked paper has a canonical id", "://" not in paper[1] and not re.search(r"v\d+$", paper[1]), paper[1])
        ck("the id survives extract_arxiv_id unchanged (now a no-op safety net)",
           dd.extract_arxiv_id(paper[1]) == paper[1], dd.extract_arxiv_id(paper[1]))
        ck("a PDF url built from it is well formed",
           f"https://arxiv.org/pdf/{paper[1]}".count("arxiv.org/pdf/") == 1)
        dd.mark_deep_dived(conn, paper[0])
        ck("mark_deep_dived succeeds (UPDATE passes the trigger)",
           conn.execute("SELECT deep_dived FROM papers WHERE id=?", (paper[0],)).fetchone()[0] == 1)
    conn.close()
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print("\nD. the wrapper's stdout contract")
src = open(os.path.expanduser("~/.hermes/scripts/run_arxiv_pipeline.sh"), encoding="utf-8").read()
ck("wrapper greps ARXIV_PULL_RESULT:", "ARXIV_PULL_RESULT:" in src)
ck("wrapper parses new=", "new=" in src)
ck("wrapper parses failed=", "failed=" in src)
ck("wrapper still reads the short report it expects generate_report to write",
   "short_$(date +%Y-%m-%d).txt" in src)
sample = "ARXIV_PULL_RESULT: new=137 failed=0 queries_run=28 last_error=none"
ck("a real result line parses to new=137",
   re.search(r"new=([0-9]+)", sample).group(1) == "137")
ck("a real result line parses to failed=0",
   re.search(r"failed=([0-9]+)", sample).group(1) == "0")
src2 = open(os.path.join(BASE, "update_arxiv.py"), encoding="utf-8").read()
ck("update_arxiv still prints that exact line", "ARXIV_PULL_RESULT: new=" in src2)
ck("the result line carries the fields the wrapper reads",
   all(f in src2 for f in ("total_new", "failures", "queries_run")))

print("\nE. cron wiring")
for s in ("run_arxiv_pipeline.sh", "run_arxiv_deep_dive.sh", "run_arxiv_eval.sh"):
    p = os.path.expanduser(f"~/.hermes/scripts/{s}")
    ck(f"{s} exists and is executable", os.path.isfile(p) and os.access(p, os.X_OK))

print("\nF. all arxiv scripts compile under the cron interpreter")
bad = []
for f in sorted(os.listdir(BASE)):
    if f.endswith(".py"):
        r = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(BASE, f)],
                           capture_output=True)
        if r.returncode != 0:
            bad.append(f)
ck(f"all scripts compile ({len([f for f in os.listdir(BASE) if f.endswith(chr(46)+chr(112)+chr(121))])} files)", not bad, bad)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
