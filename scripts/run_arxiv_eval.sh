#!/bin/bash
# Cron wrapper: arxiv interest evaluation (1st and 15th, 06:00).
#
# Contract with the scheduler:
#   * non-empty stdout  -> delivered to Telegram (suggestions to review)
#   * EMPTY stdout      -> silent run, nothing to say
#   * non-zero exit     -> run marked FAILED so a broken vault/job is visible
#
# The evaluator runs in DRY RUN: this job never writes queries.json. Applying
# a suggestion is a deliberate `python3 evaluate_interests.py --apply`.
#
# v1 fixed here: it wrote its log to /tmp, captured $? into STATUS and never
# used it (so a missing vault still reported success), and printed a
# "No new suggestions" message twice a month for four months.
set -uo pipefail

cd "$HOME/.hermes/datasets/arxiv" || exit 2

LOG_DIR="$HOME/.hermes/cache/scratch"
LOG="$LOG_DIR/arxiv_interest_eval.log"
mkdir -p "$LOG_DIR"

OUTPUT=$(python3 evaluate_interests.py --quiet 2>&1)
STATUS=$?

# keep the full run history out of the delivered message
{
    printf '[%s] exit=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$STATUS"
    printf '%s\n\n' "$OUTPUT"
} >> "$LOG"
tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

if [ "$STATUS" -ne 0 ]; then
    printf 'arxiv interest evaluator FAILED (exit %s)\n%s\n' "$STATUS" "$OUTPUT"
    exit "$STATUS"
fi

# empty output = silent run
if [ -n "$OUTPUT" ]; then
    printf '%s\n' "$OUTPUT"
fi
exit 0
