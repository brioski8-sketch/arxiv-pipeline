#!/bin/bash
# Cron wrapper: run deep dive on the highest-scoring undived paper
cd $HOME/.hermes/datasets/arxiv

# Use the ABSOLUTE system interpreter — pymupdf (fitz) is installed there.
# A bare `python3` resolves to the Hermes venv under cron, which has no fitz and
# silently skips full-text extraction. /usr/bin/python3 has pymupdf installed.
/usr/bin/python3 deep_dive.py > /tmp/arxiv_deep_dive_log.txt 2>&1
STATUS=$?

echo "=== Arxiv Deep Dive: $(date) === "
cat /tmp/arxiv_deep_dive_log.txt
echo ""
echo "Full log: /tmp/arxiv_deep_dive_log.txt"