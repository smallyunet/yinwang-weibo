# yinwang-weibo

Archive Yin Wang's Weibo posts (text + images) and generate a static website deployable to GitHub Pages.

> Note: Weibo may require login, rate limiting, or change APIs over time. This repository is intended for backing up content you are authorized to access. Please follow Weibo's rules and applicable laws.

## Quick Start (Local)

1) Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install  # Required for browser-based automation
```

2) Scrape data (writes to `data/posts.jsonl`, downloads images into `data/images/`):

Use the **browser-based automation** (recommended) to automatically get cookies and scrape.
By default, it will attempt to launch a browser (headless or visible) to initialize a session:

```bash
python scripts/scrape_weibo.py --uid 6347862377 --out data
```

**If you need to log in manually** (e.g., to scan a QR code):

```bash
python scripts/scrape_weibo.py --uid 6347862377 --out data --manual-auth
```

This will open a browser window. Log in, then press Enter in your terminal to continue.

**Alternative: Static Cookie**

If you prefer to provide a cookie manually without using Playwright:

```bash
export WEIBO_COOKIE='SUB=...; SUBP=...;'
python scripts/scrape_weibo.py --uid 6347862377 --out data
```

3) Build the static site (outputs to `docs/`):

```bash
python scripts/build_site.py --in data --out docs
```

Preview locally:

```bash
python -m http.server -d docs 8000
```

## GitHub Pages

This repo builds the site into `docs/`.

- If you deploy via GitHub Actions: set Pages Source to **GitHub Actions**.
- If scraping requires a cookie: add `WEIBO_COOKIE` in Settings → Secrets and variables → Actions.

## Data Layout

- `data/posts.jsonl`: one JSON per line (easy to append incrementally)
- `data/images/`: downloaded images organized by date/post id
# yinwang-weibo
