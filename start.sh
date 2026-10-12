#!/usr/bin/env bash
# One command to open the web app:  ./start.sh
cd "$(dirname "$0")" || exit 1
[ -d venv ] && source venv/bin/activate
git pull --ff-only -q 2>/dev/null || true      # pick up new versions if online
# prices/ belongs to the daily GitHub job. If a local copy blocked the pull, drop it and pull again.
if [ -n "$(git status --porcelain -- prices 2>/dev/null)" ]; then
  git checkout -q -- prices 2>/dev/null; git clean -fq prices 2>/dev/null
  git pull --ff-only -q 2>/dev/null || true
fi
pip install -q -r requirements.txt
echo "Opening http://localhost:8501  (Ctrl+C here to stop)"
streamlit run app.py
