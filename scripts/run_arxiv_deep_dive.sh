#!/bin/bash
# Cron wrapper: run deep dive on the highest-scoring undived paper
cd $HOME/.hermes/datasets/arxiv

# Use the ABSOLUTE system interpreter — pymupdf (fitz) is installed there.
# A bare `python3` resolves to the Hermes venv under cron, which has no fitz and
# silently skips full-text extraction. /usr/bin/python3 has pymupdf installed.
LOG_DIR="$HOME/.hermes/logs/arxiv"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/deep_dive_$(date +%F).log"
/usr/bin/python3 deep_dive.py > "$LOG" 2>&1
STATUS=$?

echo "=== Arxiv Deep Dive: $(date) === "
cat "$LOG"
echo ""
echo "Full log: $LOG"
exit "$STATUS"