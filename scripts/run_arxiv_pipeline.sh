#!/bin/bash
# Cron wrapper: update arxiv DB, generate report
cd /home/thebevans/.hermes/datasets/arxiv

# Step 1: Pull new papers
python3 update_arxiv.py > /tmp/arxiv_pull_log.txt 2>&1
PULL_STATUS=$?

# Step 1b: Pull influential (most-cited) papers — self-skips if run <7 days ago
python3 update_influential.py > /tmp/arxiv_influential_log.txt 2>&1
INFL_STATUS=$?

# Step 2: Generate enhanced report with 7-category scoring + OpenAlex enrichment
python3 generate_report.py > /tmp/arxiv_report_log.txt 2>&1
REPORT_STATUS=$?

# Step 3: Read the short report and combine with summary
SHORT_REPORT=$(cat /home/thebevans/.hermes/datasets/arxiv/reports/short_$(date +%Y-%m-%d).txt 2>/dev/null)

echo "=== Arxiv Pipeline Run: $(date) ==="
echo "Pull: ${PULL_STATUS} $(tail -1 /tmp/arxiv_pull_log.txt 2>/dev/null)"
echo "Influential: ${INFL_STATUS} $(tail -1 /tmp/arxiv_influential_log.txt 2>/dev/null)"
echo "Report: ${REPORT_STATUS} $(tail -1 /tmp/arxiv_report_log.txt 2>/dev/null)"
echo ""
echo "${SHORT_REPORT}"
echo ""
echo "Full report: /home/thebevans/.hermes/datasets/arxiv/reports/arxiv_briefing_$(date +%Y-%m-%d).md"