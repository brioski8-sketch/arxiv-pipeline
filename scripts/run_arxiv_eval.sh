#!/bin/bash
# Cron wrapper: evaluate interests (runs 1st and 15th)
cd /home/thebevans/.hermes/datasets/arxiv

python3 evaluate_interests.py > /tmp/arxiv_interest_eval.txt 2>&1
STATUS=$?

echo "=== Interest Evaluation: $(date) === "
cat /tmp/arxiv_interest_eval.txt
echo ""
echo "Full log: /tmp/arxiv_interest_eval.txt"