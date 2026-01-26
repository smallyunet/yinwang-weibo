#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# Try importing playwright for optional browser-based auth
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)


@dataclass
class WeiboPost:
    id: str
    mid: Optional[str]
    created_at: str  # ISO
    created_at_raw: str
    text_html: str
    text_plain: str
    user_id: str
    user_screen_name: Optional[str]
    source: Optional[str]
    pics: List[Dict[str, Any]]
    reposts_count: Optional[int]
    comments_count: Optional[int]
    attitudes_count: Optional[int]
    is_retweet: bool
    retweeted_status: Optional[Dict[str, Any]]


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    return soup.get_text("", strip=True)


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    return name.strip("_") or "file"


def _parse_created_at(raw: str) -> str:
    # m.weibo.cn typically returns RFC-like timestamps, e.g.
    # "Mon Jan 01 12:34:56 +0800 2024"
    try:
        dt = date_parser.parse(raw)
    except Exception:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _requests_session(cookie: Optional[str]) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "X-Requested-With": "XMLHttpRequest",
            "MWeibo-Pwa": "1",
            "Origin": "https://m.weibo.cn",
            "Referer": "https://m.weibo.cn/",
        }
    )
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def _parse_json_response(r: requests.Response) -> Dict[str, Any]:
    content_type = (r.headers.get("Content-Type") or "").lower()
    text_preview = (r.text or "")[:300]

    if r.status_code != 200:
        raise RuntimeError(
            "Non-200 response while fetching Weibo API. "
            f"status={r.status_code} url={r.url} content-type={content_type!r} preview={text_preview!r}"
        )

    # Weibo sometimes returns an empty body or an HTML block page (risk control).
    if not r.text:
        raise RuntimeError(
            "Empty response body from Weibo API. "
            f"status={r.status_code} url={r.url} content-type={content_type!r}. "
            "This is often caused by risk control (e.g., HTTP 432) or missing/expired cookies."
        )

    if "json" not in content_type and not r.text.lstrip().startswith("{"):
        raise RuntimeError(
            "Unexpected non-JSON response from Weibo API. "
            f"status={r.status_code} url={r.url} content-type={content_type!r} preview={text_preview!r}"
        )

    try:
        payload = r.json()
    except Exception as e:
        raise RuntimeError(
            "Failed to parse JSON from Weibo API response. "
            f"status={r.status_code} url={r.url} content-type={content_type!r} preview={text_preview!r}"
        ) from e

    return payload


def _ensure_ok(payload: Dict[str, Any], url: str) -> None:
    ok = payload.get("ok")
    if ok == 1:
        return
    msg = payload.get("msg") or payload.get("errmsg") or payload.get("error") or payload.get("message")
    raise RuntimeError(f"Weibo API returned ok={ok!r}. url={url} msg={msg!r} payload_keys={list(payload.keys())}")


def _extract_cards(resp_json: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    data = (resp_json or {}).get("data") or {}
    cards = data.get("cards") or []
    since_id = data.get("cardlistInfo", {}).get("since_id")
    return cards, since_id


def _iter_mblogs_from_cards(cards: List[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for card in cards:
        # Common shape: card["mblog"]
        if isinstance(card, dict) and card.get("mblog"):
            yield card["mblog"]
            continue
        # Sometimes it is wrapped under card_group
        group = card.get("card_group") if isinstance(card, dict) else None
        if isinstance(group, list):
            for c in group:
                if isinstance(c, dict) and c.get("mblog"):
                    yield c["mblog"]


def _extract_pics(mblog: Dict[str, Any]) -> List[Dict[str, Any]]:
    pics = mblog.get("pics") or []
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(pics):
        if not isinstance(p, dict):
            continue
        large = p.get("large") or {}
        url = large.get("url") or p.get("url")
        out.append({"index": i, "url": url})
    return out


def _to_post(mblog: Dict[str, Any], uid: str) -> WeiboPost:
    raw_created = mblog.get("created_at") or ""
    created_iso = _parse_created_at(raw_created)

    text_html = mblog.get("text") or ""
    text_plain = _strip_html(text_html)

    user = mblog.get("user") or {}

    retweeted = mblog.get("retweeted_status")
    is_retweet = bool(retweeted)

    return WeiboPost(
        id=str(mblog.get("id") or ""),
        mid=(str(mblog.get("mid")) if mblog.get("mid") is not None else None),
        created_at=created_iso,
        created_at_raw=raw_created,
        text_html=text_html,
        text_plain=text_plain,
        user_id=str(uid),
        user_screen_name=user.get("screen_name"),
        source=mblog.get("source"),
        pics=_extract_pics(mblog),
        reposts_count=mblog.get("reposts_count"),
        comments_count=mblog.get("comments_count"),
        attitudes_count=mblog.get("attitudes_count"),
        is_retweet=is_retweet,
        retweeted_status=retweeted,
    )


def _load_existing_ids(jsonl_path: Path) -> Set[str]:
    if not jsonl_path.exists():
        return set()
    ids: Set[str] = set()
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                pid = str(obj.get("id") or "")
                if pid:
                    ids.add(pid)
            except Exception:
                continue
    return ids


def _append_posts(jsonl_path: Path, posts: List[WeiboPost]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")


def _download_one(session: requests.Session, url: str, out_path: Path) -> bool:
    if not url:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and out_path.stat().st_size > 0:
        return True

    r = session.get(url, stream=True, timeout=30)
    if r.status_code != 200:
        return False

    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with tmp.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 64):
            if chunk:
                f.write(chunk)
    tmp.replace(out_path)
    return True


def _guess_ext_from_url(url: str) -> str:
    if not url:
        return ".jpg"
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", url, re.IGNORECASE)
    if not m:
        return ".jpg"
    ext = m.group(1).lower()
    return ".jpg" if ext == "jpeg" else f".{ext}"


def _download_pics_for_post(
    session: requests.Session,
    post: WeiboPost,
    images_root: Path,
) -> List[Dict[str, Any]]:
    # Returns pics with extra fields such as local_path
    created = date_parser.parse(post.created_at.replace("Z", "+00:00"))
    date_dir = f"{created.year:04d}/{created.month:02d}/{created.day:02d}"

    out: List[Dict[str, Any]] = []
    for pic in post.pics:
        url = pic.get("url")
        idx = pic.get("index", 0)
        ext = _guess_ext_from_url(url)
        filename = _safe_filename(f"{post.id}_{idx}{ext}")
        local_rel = Path(date_dir) / filename
        local_abs = images_root / local_rel
        ok = _download_one(session, url, local_abs)
        out.append({**pic, "local_path": str(local_rel).replace(os.sep, "/"), "downloaded": ok})
    return out


def _save_batch(jsonl_path: Path, posts: List[WeiboPost], existing_ids: Set[str], req_session: requests.Session, images_root: Path) -> None:
    # Helper to save any pending unique posts when stopping early
    unique_batch = []
    for p in posts:
        if p.id not in existing_ids:
            unique_batch.append(p)
            existing_ids.add(p.id)
    
    if unique_batch:
        for p in unique_batch:
            if p.pics:
                    p.pics = _download_pics_for_post(req_session, p, images_root)
        _append_posts(jsonl_path, unique_batch)
        print(f"Saved final batch of {len(unique_batch)} posts before stopping.")


def fetch_via_browser_intercept(
    uid: str,
    out_dir: Path,
    cookie: Optional[str],
    max_pages: Optional[int],
    sleep_min: float,
    sleep_max: float,
    stop_when_seen: bool,
    manual_auth: bool = False,
    headless: bool = False,
    max_no_new_scrolls: int = 5,
) -> None:
    if not HAS_PLAYWRIGHT:
        raise ImportError("Playwright is not installed. Please run `pip install playwright` and `playwright install`.")

    jsonl_path = out_dir / "posts.jsonl"
    images_root = out_dir / "images"
    
    # Always load existing IDs to prevent duplicates in the file
    existing_ids = _load_existing_ids(jsonl_path)
    print(f"Loaded {len(existing_ids)} existing post IDs.")

    # We need a requests session for downloading images later.
    # We will initialize it after we get cookies from the browser.
    req_session: Optional[requests.Session] = None

    with sync_playwright() as p:
        # If manual_auth is True, we must show the browser
        is_headless = headless and not manual_auth
        
        print(f"Launching browser (headless={is_headless})...")
        browser = p.chromium.launch(headless=is_headless)
        context = browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()

        # Shared state for the interceptor
        captured_posts: List[WeiboPost] = []
        
        def handle_response(response):
            if "getIndex" in response.url and response.status == 200:
                try:
                    json_body = response.json()
                    # Basic validation
                    if not isinstance(json_body, dict):
                        return
                    
                    # Parse using existing helpers
                    cards, _ = _extract_cards(json_body)
                    mblogs = list(_iter_mblogs_from_cards(cards))
                    
                    new_batch = []
                    for mblog in mblogs:
                        post = _to_post(mblog, uid)
                        if post.id and post.id not in existing_ids:
                             new_batch.append(post)

                    if new_batch:
                        captured_posts.extend(new_batch)
                        print(f"  -> Intercepted {len(new_batch)} new posts from API response.")
                except Exception:
                    # Ignore parsing errors from unrelated requests
                    pass

        # Register listener
        page.on("response", handle_response)

        url = f"https://m.weibo.cn/u/{uid}"
        print(f"Navigating to profile: {url}")
        page.goto(url, wait_until="networkidle")

        if manual_auth:
            print("-" * 60)
            print("NOTICE: Browser window is open.")
            print("Please log in if needed. The script will wait for you to press Enter.")
            print("AFTER you log in, verify you can see the content.")
            print("-" * 60)
            input("Press Enter to start scrolling and scraping...")
        else:
            print("Waiting few seconds for initial load...")
            time.sleep(3)

        # Sync cookies to requests session for image downloading
        cookies = context.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        req_session = _requests_session(cookie_str)
        print(f"Synced {len(cookies)} cookies to image downloader.")

        # Scrolling loop
        print("Starting auto-scroll...")
        
        # We track how many posts we have processed to determine if we should stop
        total_saved = 0
        consecutive_no_new = 0
        # MAX_NO_NEW_SCROLLS was 5
        
        scroll_count = 0
        
        while True:
            scroll_count += 1
            if max_pages and scroll_count > max_pages * 5: 
                # Approx conversion: 1 page ~ 5 scrolls? 
                # Let's just treat max_pages as max_scroll_blocks or ignore it.
                # User asked for ALL content, so let's be generous.
                pass

            # Scroll down
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            
            # Random wait for loading
            sleep_time = random.uniform(sleep_min, sleep_max) + 1.0 # Add bit more for network
            time.sleep(sleep_time)
            
            # Process captured posts
            if captured_posts:
                # Check for stop_when_seen BEFORE filtering
                if stop_when_seen:
                    # If any of the captured posts are already known, it implies we hit the "old" part of timeline.
                    # (Assuming reverse chronological order implementation by Weibo)
                    for p in captured_posts:
                        if p.id in existing_ids:
                            print(f"Encountered existing post {p.id}. Stopping (stop-when-seen).")
                            _save_batch(jsonl_path, captured_posts, existing_ids, req_session, images_root)
                            return

                # Deduplicate and save
                unique_batch = []
                for p in captured_posts:
                    if p.id not in existing_ids:
                        unique_batch.append(p)
                        existing_ids.add(p.id)
                
                captured_posts.clear() # Clear buffer
                
                if unique_batch:
                    # Download images
                    for p in unique_batch:
                        if p.pics:
                             p.pics = _download_pics_for_post(req_session, p, images_root)
                    
                    _append_posts(jsonl_path, unique_batch)
                    total_saved += len(unique_batch)
                    print(f"Scroll {scroll_count}: Saved {len(unique_batch)} posts. (Total: {total_saved})")
                    consecutive_no_new = 0
                else:
                    print(f"Scroll {scroll_count}: No new unique posts found (duplicates filtered).")
                    consecutive_no_new += 1
            else:
                print(f"Scroll {scroll_count}: No API responses intercepted.")
                consecutive_no_new += 1

            if consecutive_no_new >= max_no_new_scrolls:
                print(f"No new content for {max_no_new_scrolls} consecutive scrolls. Stopping.")
                break
                
            # If "End" text is visible, maybe stop? 
            # Weibo mobile usually just stops loading.
            
        print(f"Done. Total posts saved: {total_saved}")
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", default="6347862377")
    parser.add_argument("--out", default="data")
    parser.add_argument("--cookie", default=None, help="Cookie string (used for API-only mode).")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--sleep-min", type=float, default=1.0)
    parser.add_argument("--sleep-max", type=float, default=2.5)
    parser.add_argument(
        "--stop-when-seen",
        action="store_true",
        help="Stop when reaching an existing post ID",
    )
    
    parser.add_argument(
        "--manual-auth",
        action="store_true",
        help="Open browser logic with manual login pause.",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Force using the old API-only method (no browser scrolling).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default False for debugging/visibility).",
    )
    parser.add_argument(
        "--max-no-new-scrolls",
        type=int,
        default=5,
        help="Stop after this many scrolls with no new content (default 5). Set higher (e.g. 100) to force deeper scrolling.",
    )

    args = parser.parse_args()

    out_dir = Path(args.out)
    
    # Decide mode
    # Default to Browser Intercept because API is flaky
    use_browser = True
    if args.api_only:
        use_browser = False
    elif not HAS_PLAYWRIGHT:
        print("Playwright not installed, falling back to API-only mode.")
        use_browser = False

    if use_browser:
        print("Using Browser Intercept Mode (Recommended).")
        # headless default: 
        # If manual-auth -> False
        # If not manual-auth -> True if set, else False (wait, user might want to see it running)
        # Let's keep it visible by default as user requested "directly open browser".
        is_headless = args.headless
        if args.manual_auth:
             is_headless = False

        fetch_via_browser_intercept(
            uid=str(args.uid),
            out_dir=out_dir,
            cookie=None, # Cookie will be grabbed from browser
            max_pages=args.max_pages,
            sleep_min=args.sleep_min,
            sleep_max=args.sleep_max,
            stop_when_seen=bool(args.stop_when_seen),
            manual_auth=args.manual_auth,
            headless=is_headless,
            max_no_new_scrolls=args.max_no_new_scrolls,
        )
    else:
        print("Using Legacy API Mode.")
        cookie = args.cookie or os.environ.get("WEIBO_COOKIE")
        fetch_all(
            uid=str(args.uid),
            out_dir=out_dir,
            cookie=cookie,
            max_pages=args.max_pages,
            sleep_min=args.sleep_min,
            sleep_max=args.sleep_max,
            stop_when_seen=bool(args.stop_when_seen),
        )


if __name__ == "__main__":
    main()
