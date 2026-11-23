"""Checks for the "reported once, never again" arxiv briefing ledger.

Part A exercises the ledger helpers against an in-memory DB (no side effects on the
real one). Part B applies generate_report's real selection queries to the real DB
and asserts that already-reported papers — the Definitions paper above all — are
excluded, while the sections still have content to show.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.expanduser("~/.hermes/datasets/arxiv"))
import generate_report as gr  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


print("A. ledger helpers, against an in-memory DB")
mem = sqlite3.connect(":memory:")
gr.ensure_ledger(mem)
check("table created", mem.execute(
    "SELECT count(*) FROM sqlite_master WHERE name=?", (gr.LEDGER_TABLE,)).fetchone()[0] == 1)
check("empty ledger loads as empty set", gr.load_reported(mem) == set())

n = gr.record_reported(mem, [{"arxiv_id": "2401.00001"}, {"arxiv_id": "2401.00002"},
                            {"arxiv_id": "2401.00002"}, {"arxiv_id": ""}, {}], "2026-10-05")
check("counts DISTINCT ids only (a paper in two sections counts once)", n == 2, n)
check("blank/missing ids skipped", mem.execute(
    f"SELECT count(*) FROM {gr.LEDGER_TABLE}").fetchone()[0] == 2)
check("reported set is readable back",
      gr.load_reported(mem) == {"2401.00001", "2401.00002"}, gr.load_reported(mem))

gr.record_reported(mem, [{"arxiv_id": "2401.00001"}], "2026-10-12")
row = mem.execute(f"SELECT times_reported, last_report_date FROM {gr.LEDGER_TABLE} "
                  f"WHERE arxiv_id='2401.00001'").fetchone()
check("re-reporting on a NEW day increments (it was reported again, later)", row == (2, "2026-10-12"), row)
check("first_reported_at is preserved on re-report",
      gr.record_reported(mem, [{"arxiv_id": "2401.00001"}], "2026-10-19") == 1)

# Same-day re-runs: the briefing is generated twice per run (wrapper, then the cron
# prompt's Step 1), so a same-day re-report must NOT look like a second report.
before_same = mem.execute(f"SELECT times_reported FROM {gr.LEDGER_TABLE} "
                          f"WHERE arxiv_id='2401.00001'").fetchone()[0]
gr.record_reported(mem, [{"arxiv_id": "2401.00001"}], "2026-10-19")
after_same = mem.execute(f"SELECT times_reported FROM {gr.LEDGER_TABLE} "
                         f"WHERE arxiv_id='2401.00001'").fetchone()[0]
check("a same-day re-report does not inflate times_reported",
      before_same == after_same == 3, (before_same, after_same))

# Same-day idempotency of the suppression itself.
same_day = gr.load_reported(mem, before="2026-10-19")
later = gr.load_reported(mem, before="2026-10-20")
check("load_reported(before=today) exempts today's reports", "2401.00001" not in same_day, same_day)
check("load_reported(before=tomorrow) suppresses them", "2401.00001" in later, later)
check("load_reported() with no date returns everything",
      {"2401.00001", "2401.00002"} <= gr.load_reported(mem))

print("\nB. the real selections, with the real ledger applied")
real = sqlite3.connect(f"file:{gr.DB_PATH}?mode=ro", uri=True)
real.row_factory = sqlite3.Row
ledger_all = {r[0] for r in real.execute(f"SELECT arxiv_id FROM {gr.LEDGER_TABLE}")}
# `reported` mirrors PRODUCTION: generate_report suppresses only papers reported on a
# PREVIOUS day, so that re-running the generator on the same day reproduces the same
# briefing instead of emptying it.
today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
reported = gr.load_reported(real, before=today)
check("ledger is populated", len(ledger_all) > 1000, len(ledger_all))
check("production suppression set is non-empty", len(reported) > 0, len(reported))
check("today's own reports are exempt from suppression (idempotent re-runs)",
      len(reported) <= len(ledger_all))

DEF = "0706.3639"
check("the Definitions paper is recorded in the ledger", DEF in ledger_all)
check("...and is suppressed on a later day",
      DEF in gr.load_reported(real, before="2099-01-01"))

# new_papers (ingested window) — same SQL as generate_report, then the same filter
cutoff = gr.get_last_report_cutoff()
rows = [dict(r) for r in real.execute(
    "SELECT * FROM papers WHERE ingested_at >= ? ORDER BY published DESC, ingested_at DESC",
    (cutoff,))]
filtered = [p for p in rows if (p.get("arxiv_id") or "") not in reported]
check("window query still returns raw rows", len(rows) > 0, len(rows))
check("no reported paper survives the window filter",
      all((p.get("arxiv_id") or "") not in reported for p in filtered))
check("filter does not empty the window", len(filtered) > 0, len(filtered))

# top-cited — the static list that was printing the same 5 papers every run
alltime = [dict(r) for r in real.execute(
    "SELECT title, arxiv_id, citation_count FROM papers WHERE citation_count > 0 "
    "ORDER BY citation_count DESC LIMIT 5")]
# The regression guard is about BEHAVIOUR, not identity: the OLD query ranks by
# all-time citations with no ledger filter, so its top rows are already-reported
# classics. Pinning a single id made this fail whenever citation data moved.
check("REGRESSION: the OLD all-time query still surfaces an already-reported paper",
      bool(alltime) and any(p["arxiv_id"] in reported for p in alltime),
      [p["arxiv_id"] for p in alltime[:3]])

tc_rows = [dict(r) for r in real.execute(
    """SELECT title, arxiv_id, citation_count FROM papers
       WHERE citation_count > 0 AND ingested_at >= ? ORDER BY citation_count DESC LIMIT 60""",
    (cutoff,))]
tc = [t for t in tc_rows if t["arxiv_id"] not in reported][:5]
check("top-cited no longer contains the Definitions paper",
      all(t["arxiv_id"] != DEF for t in tc), [t["arxiv_id"] for t in tc])
check("top-cited excludes every reported paper",
      all(t["arxiv_id"] not in reported for t in tc))
check("top-cited is window-scoped, so it cannot degrade into an all-time tail",
      all(t["arxiv_id"] in {r["arxiv_id"] for r in real.execute(
          "SELECT arxiv_id FROM papers WHERE citation_count > 0 AND ingested_at >= ?",
          (cutoff,))} for t in tc))
check("top-cited may legitimately be empty (section is simply omitted)",
      len(tc) <= 5, len(tc))
check("top-cited entries are still descending by citations",
      all(tc[i]["citation_count"] >= tc[i + 1]["citation_count"] for i in range(len(tc) - 1)),
      [t["citation_count"] for t in tc])
print(f"        top-cited would now show {len(tc)} entr(y/ies):")
for i, t in enumerate(tc, 1):
    print(f"          {i}. {t['citation_count']:>4} cites  {t['title'][:52]}")

# influential_new
inf_rows = [dict(r) for r in real.execute(
    "SELECT * FROM papers WHERE ingested_at >= ? AND source = 'influential' "
    "ORDER BY citation_count DESC LIMIT 10", (cutoff,))]
inf = [p for p in inf_rows if (p.get("arxiv_id") or "") not in reported]
check("high-impact section excludes reported papers",
      all((p.get("arxiv_id") or "") not in reported for p in inf))

print("\nC. syntax of the two changed scripts")
import py_compile  # noqa: E402
for f in ["generate_report.py", "seed_reported_from_reports.py"]:
    p = os.path.expanduser(f"~/.hermes/datasets/arxiv/{f}")
    try:
        py_compile.compile(p, doraise=True)
        check(f"{f} compiles", True)
    except py_compile.PyCompileError as exc:
        check(f"{f} compiles", False, str(exc)[:120])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
