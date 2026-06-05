#!/usr/bin/env bash
set -euo pipefail
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

SUMMON_DIR="/Users/4jp/Workspace/4444J99/summoning"
WORK_DIR="/Users/4jp/_doc"                      # summon.py hardcoded working output (DOC_DIR in summon.py)
MANIFEST_REPO="/Users/4jp/_portal/config/_doc"  # canonical manifest home since 2026-06-01 consolidation
LOG_DIR="/tmp/summon-daily-logs"
RETENTION=7

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d).log"

exec >"$LOG_FILE" 2>&1
echo "=== summon-daily: $(date) ==="

cd "$SUMMON_DIR"
python3 summon.py run --scrub-secrets 2>&1

# Publish the manifest into the canonical git repo (~/_doc was de-gitted 2026-06-01;
# committing there failed silently every morning — see .MOVED-TO.md breadcrumbs).
cp "$WORK_DIR/manifest.jsonl" "$MANIFEST_REPO/manifest.jsonl"
cd "$MANIFEST_REPO"
git add manifest.jsonl
if git diff --cached --quiet; then
  echo "Nothing to commit."
else
  git commit -m "docs: daily archive update $(date +%Y-%m-%d)"
  git push --no-verify 2>&1
  echo "Pushed."
fi

python3 "$SUMMON_DIR/summon.py" status 2>&1

find "$LOG_DIR" -name '*.log' -mtime +$RETENTION -delete
echo "=== done: $(date) ==="
