#!/bin/bash
# Cron wrapper: run deep dive on the highest-scoring undived paper
cd /home/thebevans/.hermes/datasets/arxiv

# Use python3 (system default) — pymupdf (fitz) is installed there.
# python3.12 lost fitz after an update; using it silently skips full-text extraction.
python3 deep_dive.py > /tmp/arxiv_deep_dive_log.txt 2>&1
STATUS=$?

echo "=== Arxiv Deep Dive: $(date) === "
cat /tmp/arxiv_deep_dive_log.txt
echo ""
echo "Full log: /tmp/arxiv_deep_dive_log.txt"