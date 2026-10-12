#!/usr/bin/env bash
# One command to open the web app:  ./start.sh
cd "$(dirname "$0")" || exit 1
[ -d venv ] && source venv/bin/activate
git pull --ff-only -q 2>/dev/null || true      # pick up new versions if online
pip install -q -r requirements.txt
echo "Opening http://localhost:8501  (Ctrl+C here to stop)"
streamlit run app.py
