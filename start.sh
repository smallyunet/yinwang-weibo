#!/usr/bin/env bash

set -euo pipefail

PYTHON_BIN="python"
if [ -x ".venv/bin/python" ]; then
	if .venv/bin/python - <<'PY' >/dev/null 2>&1
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
	browser = playwright.chromium.launch(headless=True)
	browser.close()
PY
	then
		PYTHON_BIN=".venv/bin/python"
	else
		echo "Project .venv exists, but Playwright is not fully ready there; falling back to shell python." >&2
		echo "Run '.venv/bin/python -m playwright install chromium' later to finish local setup." >&2
	fi
fi

"$PYTHON_BIN" scripts/scrape_weibo.py --uid 6347862377 --out data --max-pages 2