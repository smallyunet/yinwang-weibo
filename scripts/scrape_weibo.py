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
    is_long_text: bool = False  # New field


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
        is_long_text=bool(mblog.get("isLongText")),
    )


def _post_from_dict(obj: Dict[str, Any]) -> Optional[WeiboPost]:
    try:
        return WeiboPost(**obj)
    except Exception:
        return None


def _load_existing_posts(jsonl_path: Path) -> Dict[str, WeiboPost]:
    if not jsonl_path.exists():
        return {}
    posts: Dict[str, WeiboPost] = {}
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            post = _post_from_dict(obj)
            if post and post.id:
                posts[post.id] = post
    return posts


def _load_existing_ids(jsonl_path: Path) -> Set[str]:
    return set(_load_existing_posts(jsonl_path).keys())


def _append_posts(jsonl_path: Path, posts: List[WeiboPost]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")


def _download_one(session: requests.Session, url: str, out_path: Path, overwrite: bool = False) -> bool:
    if not url:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and out_path.stat().st_size > 0 and not overwrite:
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
    overwrite: bool = False,
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
        ok = _download_one(session, url, local_abs, overwrite=overwrite)
        out.append({**pic, "local_path": str(local_rel).replace(os.sep, "/"), "downloaded": ok})
    return out


def _normalize_pics_for_compare(pics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for pic in pics or []:
        if not isinstance(pic, dict):
            continue
        normalized.append(
            {
                "index": pic.get("index"),
                "url": pic.get("url"),
            }
        )
    return normalized


def _post_compare_dict(post: WeiboPost) -> Dict[str, Any]:
    data = asdict(post)
    data["pics"] = _normalize_pics_for_compare(post.pics)
    return data


def _post_has_changed(existing: WeiboPost, incoming: WeiboPost) -> bool:
    return _post_compare_dict(existing) != _post_compare_dict(incoming)


def _existing_pic_lookup(post: WeiboPost) -> Dict[Tuple[Any, Any], Dict[str, Any]]:
    lookup: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for pic in post.pics or []:
        if not isinstance(pic, dict):
            continue
        key = (pic.get("index"), pic.get("url"))
        lookup[key] = pic
    return lookup


def _merge_existing_pic_metadata(existing: WeiboPost, incoming: WeiboPost) -> List[Dict[str, Any]]:
    existing_lookup = _existing_pic_lookup(existing)
    merged: List[Dict[str, Any]] = []
    for pic in incoming.pics or []:
        if not isinstance(pic, dict):
            continue
        key = (pic.get("index"), pic.get("url"))
        existing_pic = existing_lookup.get(key)
        if existing_pic:
            merged.append({**pic, **{k: v for k, v in existing_pic.items() if k not in {"index", "url"}}})
        else:
            merged.append(pic)
    return merged


def _prepare_post_media(
    post: WeiboPost,
    existing_post: Optional[WeiboPost],
    req_session: Optional[requests.Session],
    images_root: Path,
    redownload_images_on_update: bool,
) -> None:
    if not post.pics:
        return
    if existing_post and not redownload_images_on_update:
        post.pics = _merge_existing_pic_metadata(existing_post, post)
    if req_session:
        should_overwrite = bool(existing_post and redownload_images_on_update)
        post.pics = _download_pics_for_post(req_session, post, images_root, overwrite=should_overwrite)


def _upsert_posts(
    jsonl_path: Path,
    posts: List[WeiboPost],
    existing_posts: Dict[str, WeiboPost],
    req_session: Optional[requests.Session],
    images_root: Path,
    redownload_images_on_update: bool,
) -> Tuple[int, int]:
    batch_by_id: Dict[str, WeiboPost] = {}
    for post in posts:
        if post.id:
            batch_by_id[post.id] = post

    inserted = 0
    updated = 0
    changed = False

    for post in batch_by_id.values():
        existing_post = existing_posts.get(post.id)
        if existing_post is None:
            _prepare_post_media(post, None, req_session, images_root, redownload_images_on_update)
            existing_posts[post.id] = post
            inserted += 1
            changed = True
            continue

        if not _post_has_changed(existing_post, post):
            continue

        _prepare_post_media(post, existing_post, req_session, images_root, redownload_images_on_update)
        existing_posts[post.id] = post
        updated += 1
        changed = True

    if changed:
        ordered_posts = sorted(
            existing_posts.values(),
            key=lambda p: (p.created_at, p.id),
        )
        _rewrite_jsonl(jsonl_path, ordered_posts)

    return inserted, updated


def _fetch_long_text(session: requests.Session, pid: str) -> Optional[str]:
    """
    Fetches the full long text content for a post.
    Returns the HTML content or None if failed.
    """
    url = f"https://m.weibo.cn/statuses/extend?id={pid}"
    try:
        r = session.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # data['data']['longTextContent']
            return (data.get("data") or {}).get("longTextContent")
    except Exception as e:
        print(f"Warning: Failed to fetch long text for {pid}: {e}")
    return None


def _save_batch(
    jsonl_path: Path,
    posts: List[WeiboPost],
    existing_posts: Dict[str, WeiboPost],
    req_session: Optional[requests.Session],
    images_root: Path,
    redownload_images_on_update: bool,
) -> Tuple[int, int]:
    pending_posts = [p for p in posts if p.id]
    if not pending_posts:
        return 0, 0

    for p in pending_posts:
        if p.is_long_text and req_session:
            print(f"  Fetching long text for {p.id}...")
            long_html = _fetch_long_text(req_session, p.id)
            if long_html:
                p.text_html = long_html
                p.text_plain = _strip_html(long_html)
                time.sleep(0.5)

    inserted, updated = _upsert_posts(
        jsonl_path,
        pending_posts,
        existing_posts,
        req_session,
        images_root,
        redownload_images_on_update,
    )
    if inserted or updated:
        print(f"Saved final batch before stopping. inserted={inserted}, updated={updated}.")
    return inserted, updated


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
    redownload_images_on_update: bool = False,
) -> None:
    if not HAS_PLAYWRIGHT:
        raise ImportError("Playwright is not installed. Please run `pip install playwright` and `playwright install`.")

    jsonl_path = out_dir / "posts.jsonl"
    images_root = out_dir / "images"
    
    existing_posts = _load_existing_posts(jsonl_path)
    print(f"Loaded {len(existing_posts)} existing posts.")

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
                        if post.id:
                            new_batch.append(post)

                    if new_batch:
                        captured_posts.extend(new_batch)
                        print(f"  -> Intercepted {len(new_batch)} posts from API response.")
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

            # Human-like scrolling behavior
            # 1. Scroll down variable amount (mostly one screen)
            scroll_y = random.randint(800, 1500)
            page.evaluate(f"window.scrollBy(0, {scroll_y})")
            
            # 2. Randomly move mouse to simulate reading/interaction
            try:
                page.mouse.move(random.randint(10, 300), random.randint(10, 800))
            except Exception:
                pass
                
            # 3. Occasional small scroll up (simulate re-reading) - 10% chance
            if random.random() < 0.1:
                time.sleep(1)
                page.evaluate("window.scrollBy(0, -300)")
                time.sleep(1)
                page.evaluate("window.scrollBy(0, 300)")

            # Random wait for loading (Human read time)
            sleep_time = random.uniform(sleep_min, sleep_max)
            # Add extra delays occasionally
            if random.random() < 0.05:
                # "Long pause"
                print("  (Simulating user reading... pausing for 5s)")
                sleep_time += 5.0
            
            time.sleep(sleep_time)
            
            # Process captured posts
            if captured_posts:
                # Check for stop_when_seen BEFORE filtering
                if stop_when_seen:
                    # If any of the captured posts are already known, it implies we hit the "old" part of timeline.
                    # (Assuming reverse chronological order implementation by Weibo)
                    for p in captured_posts:
                        if p.id in existing_posts:
                            print(f"Encountered existing post {p.id}. Stopping (stop-when-seen).")
                            _save_batch(
                                jsonl_path,
                                captured_posts,
                                existing_posts,
                                req_session,
                                images_root,
                                redownload_images_on_update,
                            )
                            return

                batch = captured_posts[:]
                captured_posts.clear()

                inserted, updated = _save_batch(
                    jsonl_path,
                    batch,
                    existing_posts,
                    req_session,
                    images_root,
                    redownload_images_on_update,
                )

                if inserted or updated:
                    total_saved += inserted
                    print(
                        f"Scroll {scroll_count}: inserted={inserted}, updated={updated}. "
                        f"(Total inserted: {total_saved})"
                    )
                    consecutive_no_new = 0
                else:
                    print(f"Scroll {scroll_count}: No inserted or updated posts found.")
                    consecutive_no_new += 1
            else:
                print(f"Scroll {scroll_count}: No API responses intercepted.")
                consecutive_no_new += 1
            
            if consecutive_no_new > 2:
                print("  [Tip] If you see a CAPTCHA in the browser, please solve it manually ASAP.")


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
    parser.add_argument("--sleep-min", type=float, default=2.0)
    parser.add_argument("--sleep-max", type=float, default=5.0)
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
def fix_existing_data(out_dir: Path, manual_auth: bool, headless: bool, uid: str) -> None:
    """
    Scans existing posts.jsonl and refetches full text for truncated posts.
    """
    if not HAS_PLAYWRIGHT:
         raise ImportError("Playwright is required for this mode.")

    jsonl_path = out_dir / "posts.jsonl"
    if not jsonl_path.exists():
        print("No data found to fix.")
        return

    # Load all posts
    posts: List[WeiboPost] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                posts.append(WeiboPost(**json.loads(line)))
            except Exception:
                pass
    
    print(f"Loaded {len(posts)} posts. Scanning for truncated content...")
    
    # Identify candidates
    candidates = []
    for p in posts:
        # Check explicit flag OR heuristic
        # Heuristic: text ends with "全文" link or looks truncated
        # The Weibo link text is usually <a href="...">全文</a>
        needs_fix = p.is_long_text
        if not needs_fix and "全文" in (p.text_html or ""):
             needs_fix = True
        
        if needs_fix:
            candidates.append(p)
            
    if not candidates:
        print("No truncated posts found.")
        return
        
    print(f"Found {len(candidates)} posts that might be truncated. Initializing browser for API session...")
    
    # Get session
    with sync_playwright() as p:
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
        
        # Go to profile to get cookie and look correct
        url = f"https://m.weibo.cn/u/{uid}"
        print(f"Navigating to profile: {url}")
        page.goto(url, wait_until="networkidle")
        
        if manual_auth:
             print("-" * 60)
             print("Please log in if needed. Press Enter when ready.")
             print("-" * 60)
             input("Press Enter to continue...")
        else:
             time.sleep(3)
             
        cookies = context.cookies()
        if not cookies:
             # Retry
             time.sleep(2)
             cookies = context.cookies()
             
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        req_session = _requests_session(cookie_str)
        print(f"Session initialized with {len(cookies)} cookies.")
        
        fix_count = 0
        consecutive_failures = 0
        
        for i, post in enumerate(candidates):
            old_len = len(post.text_plain or "")
            print(f"[{i+1}/{len(candidates)}] Fixing post {post.id} (len: {old_len})...", end="", flush=True)
            
            long_html = _fetch_long_text(req_session, post.id)
            if long_html:
                post.text_html = long_html
                post.text_plain = _strip_html(long_html)
                post.is_long_text = True 
                fix_count += 1
                consecutive_failures = 0  # Reset
                new_len = len(post.text_plain)
                print(f" -> Expanded to {new_len} chars.")
                
                # Save periodically to prevent data loss
                if fix_count % 10 == 0:
                     _rewrite_jsonl(jsonl_path, posts)
                     print("  (Auto-saved progress)")

                time.sleep(random.uniform(2.0, 4.0)) # Slower pace
            else:
                print(" -> Failed to fetch.")
                consecutive_failures += 1
                
                if consecutive_failures >= 3:
                    print("\n[Warning] Too many consecutive failures. API might be rate-limiting.")
                    print("Cooling down for 60 seconds... (Do not close)")
                    time.sleep(60)
                    consecutive_failures = 0 # Reset to try again
                else:
                    time.sleep(2)
                
        browser.close()
        
    print(f"Fixed {fix_count} posts. Saving...")
    _rewrite_jsonl(jsonl_path, posts)
    print("Done.")


def _rewrite_jsonl(path: Path, posts: List[WeiboPost]) -> None:
    # Backup first
    bak = path.with_suffix(".jsonl.bak")
    if path.exists():
         import shutil
         shutil.copy(path, bak)
    
    with path.open("w", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", default="6347862377")
    parser.add_argument("--out", default="data")
    parser.add_argument("--cookie", default=None, help="Cookie string (used for API-only mode).")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--sleep-min", type=float, default=2.0)
    parser.add_argument("--sleep-max", type=float, default=5.0)
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
    parser.add_argument(
        "--fix-long-text",
        action="store_true",
        help="Scan existing data and fetch full text for truncated posts.",
    )
    parser.add_argument(
        "--redownload-images-on-update",
        action="store_true",
        help="When an existing post changes, re-download its images instead of reusing existing local files.",
    )

    args = parser.parse_args()

    out_dir = Path(args.out)
    
    if args.fix_long_text:
        print("Running in FIX LONG TEXT mode.")
        # Need to decide headless info
        is_headless = args.headless
        if args.manual_auth:
             is_headless = False
             
        fix_existing_data(out_dir, args.manual_auth, is_headless, str(args.uid))
        return

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
            redownload_images_on_update=args.redownload_images_on_update,
        )
    else:
        raise RuntimeError(
            "Legacy API mode is not available in this script. "
            "Use the default browser intercept mode, or implement fetch_all before using --api-only."
        )


if __name__ == "__main__":
    main()
