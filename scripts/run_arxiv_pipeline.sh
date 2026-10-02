#!/bin/bash
# Cron wrapper: update arxiv DB -> refresh influential/most-cited -> generate report.
#
# HARD BUDGET: cron.script_timeout_seconds = 600 in config.yaml. The whole
# wrapper must finish inside that, so every step is individually bounded and a
# slow/throttled step can NEVER take the report down with it. `timeout` exits
# 124 when it fires — reported below rather than silently swallowed.
#
# Order is deliberate and must not change: update_influential.py runs straight
# after the fresh pull so generate_report.py can rank the new papers by citation
# impact in the same run.
#
# `python3 -u` everywhere: Python block-buffers stdout when redirected, so the
# logs used to sit at 0 bytes during a hang and the failure looked silent.
#
# All arXiv/OpenAlex requests identify via mailto (see ARXIV_MAILTO / the
# MAILTO constants in each script). Override with ARXIV_MAILTO=you@example.com.
cd "$HOME/.hermes/datasets/arxiv"

PULL_LOG=/tmp/arxiv_pull_log.txt
INFL_LOG=/tmp/arxiv_influential_log.txt
REPORT_LOG=/tmp/arxiv_report_log.txt

# Step 1: Pull new papers (~28 queries, 3.5s apart; aborts early if arXiv stays down).
#         Budget 270s vs the script's own 240s PULL_TIME_BUDGET, so retries are
#         clamped by the script and `timeout` stays the outer backstop.
timeout 270 python3 -u update_arxiv.py > "$PULL_LOG" 2>&1
PULL_STATUS=$?

# arXiv ToU: "no more than one request every three seconds" applies across ALL
# of this client's requests, not per-script. Each script spaces its own queries,
# but the handoff from step 1 to step 1b is still a back-to-back pair — wait it
# out here so the collective rate never dips below the limit.
sleep 5

# Step 1b: Influential (most-cited) papers for the fresh batch. Self-skips if
# run <7 days ago; self-bounds to TIME_BUDGET internally.
timeout 180 python3 -u update_influential.py > "$INFL_LOG" 2>&1
INFL_STATUS=$?

# Step 2: Generate the scored report — runs even if 1/1b were killed above.
timeout 120 python3 -u generate_report.py > "$REPORT_LOG" 2>&1
REPORT_STATUS=$?

# Step 3: Read the short report and combine with summary
SHORT_REPORT=$(cat "$HOME/.hermes/datasets/arxiv/reports/short_$(date +%Y-%m-%d).txt" 2>/dev/null)

status_note() {
    # 124 = `timeout` killed it; 0 = clean; anything else = real error.
    case "$1" in
        0)   echo "ok" ;;
        124) echo "TIMED OUT (step budget exceeded)" ;;
        *)   echo "FAILED rc=$1" ;;
    esac
}

# Parse the machine-readable result update_arxiv.py prints as its last line.
# A genuinely quiet week (failed=0) and a dead pull (failed>0) used to be
# indistinguishable, which is how the 2026-09-28 total collection failure
# reached the user as silence instead of an alert.
PULL_RESULT=$(grep -a "ARXIV_PULL_RESULT:" "$PULL_LOG" 2>/dev/null | tail -1)
PULL_NEW=$(printf '%s' "$PULL_RESULT" | sed -n 's/.*new=\([0-9][0-9]*\).*/\1/p')
PULL_FAILED=$(printf '%s' "$PULL_RESULT" | sed -n 's/.*failed=\([0-9][0-9]*\).*/\1/p')
PULL_NEW=${PULL_NEW:-0}
PULL_FAILED=${PULL_FAILED:-0}

echo "=== Arxiv Pipeline Run: $(date) ==="
echo "Pull:        $(status_note "$PULL_STATUS") -- $(tail -1 "$PULL_LOG" 2>/dev/null)"
echo "Influential: $(status_note "$INFL_STATUS") -- $(tail -1 "$INFL_LOG" 2>/dev/null)"
echo "Report:      $(status_note "$REPORT_STATUS") -- $(tail -1 "$REPORT_LOG" 2>/dev/null)"
echo ""
if [ "$PULL_FAILED" -gt 0 ] || [ "$PULL_STATUS" -ne 0 ]; then
    echo "*** COLLECTION FAILURE — DO NOT REPORT THIS AS A QUIET WEEK ***"
    if [ "$PULL_FAILED" -gt 0 ]; then
        echo "arXiv query failures means the pull did not complete: ${PULL_FAILED} queries"
        echo "failed after retries and ${PULL_NEW} new papers were ingested. Any 'no new"
        echo "papers' message below is an artefact of the failure, not a real result."
        echo "Full fetch errors:"
        grep -a -E "ERROR fetching|ABORT:|gave up after" "$PULL_LOG" 2>/dev/null | tail -20
    else
        echo "The pull script exited rc=${PULL_STATUS} (124 = hard timeout) without a"
        echo "completion summary, so the pull did not finish. Treat any 'no new papers'"
        echo "message below as an artefact of the failure, not a real result."
        echo "Last lines of the pull log:"
        tail -20 "$PULL_LOG" 2>/dev/null
    fi
    echo "*** END COLLECTION FAILURE ***"
    echo ""
fi
if [ -z "$SHORT_REPORT" ]; then
    echo "WARNING: no short report for $(date +%F) — generate_report.py did not write one."
    echo "Last 15 lines of the report log:"
    tail -15 "$REPORT_LOG" 2>/dev/null
fi
echo "${SHORT_REPORT}"
echo ""
echo "Full report: $HOME/.hermes/datasets/arxiv/reports/arxiv_briefing_$(date +%Y-%m-%d).md"
exit 0
