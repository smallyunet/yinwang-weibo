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


def get_cookie_from_browser(uid: str, manual_auth: bool = False, headless: bool = False) -> str:
    """
    Launches a browser (Playwright), navigates to the user's profile to initialize session/cookies,
    and returns the cookie string.
    """
    if not HAS_PLAYWRIGHT:
        raise ImportError("Playwright is not installed. Please run `pip install playwright` and `playwright install`.")

    with sync_playwright() as p:
        # If manual_auth is True, we must show the browser (headless=False)
        # otherwise respect the 'headless' arg.
        is_headless = headless and not manual_auth
        
        print(f"Launching browser (headless={is_headless})...")
        browser = p.chromium.launch(headless=is_headless)
        context = browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 390, "height": 844},  # Mobile view
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        
        url = f"https://m.weibo.cn/u/{uid}"
        print(f"Navigating to profile: {url}")
        page.goto(url, wait_until="networkidle")
        
        if manual_auth:
            print("-" * 60)
            print("NOTICE: Browser window is open.")
            print("Please log in or verify you can see the content in the browser window.")
            print("When ready, press Enter here to capture cookies and proceed.")
            print("-" * 60)
            input("Press Enter to continue...")
        else:
            # Wait a bit to ensure potential guest session or risk control checks pass
            print("Waiting 5 seconds for session initialization...")
            time.sleep(5)
            
        cookies = context.cookies()
        if not cookies:
             # retry once
             time.sleep(2)
             cookies = context.cookies()

        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        print(f"Captured {len(cookies)} cookies.")
        
        browser.close()
        return cookie_str


def fetch_all(
    uid: str,
    out_dir: Path,
    cookie: Optional[str],
    max_pages: Optional[int],
    sleep_min: float,
    sleep_max: float,
    stop_when_seen: bool,
) -> None:
    jsonl_path = out_dir / "posts.jsonl"
    images_root = out_dir / "images"

    existing_ids = _load_existing_ids(jsonl_path) if stop_when_seen else set()

    session = _requests_session(cookie)

    containerid = f"107603{uid}"
    base_url = "https://m.weibo.cn/api/container/getIndex"

    since_id: Optional[str] = None
    page = 0
    new_count = 0

    while True:
        page += 1
        params = {
            "type": "uid",
            "value": uid,
            "containerid": containerid,
        }
        if since_id:
            params["since_id"] = since_id

        r = session.get(base_url, params=params, timeout=30)
        
        try:
             payload = _parse_json_response(r)
        except RuntimeError as e:
            # If we fail and we are not using browser, maybe warn?
            # But the caller might want to know.
            print(f"Error fetching page {page}: {e}")
            break

        try:
            _ensure_ok(payload, r.url)
        except RuntimeError as e:
            print(f"Error checking API ok status on page {page}: {e}")
            break

        cards, next_since_id = _extract_cards(payload)

        if not cards:
            print(f"No cards found on page {page}. Stopping.")
            break

        mblogs = list(_iter_mblogs_from_cards(cards))
        if not mblogs:
            print(f"No mblog entries found on page {page}. Stopping.")
            break

        posts: List[WeiboPost] = []
        for mblog in mblogs:
            post = _to_post(mblog, uid)
            if not post.id:
                continue

            if stop_when_seen and post.id in existing_ids:
                # When paging from newest to oldest, once we hit an existing ID,
                # the remaining pages are typically all older. Stop to avoid re-fetching.
                _append_posts(jsonl_path, posts)
                print(f"Encoutered existing post {post.id}. Stopping (stop-when-seen).")
                return

            # Download images and store local paths
            if post.pics:
                post.pics = _download_pics_for_post(session, post, images_root)

            posts.append(post)
            new_count += 1
        
        if posts:
            _append_posts(jsonl_path, posts)
            print(f"Page {page}: saved {len(posts)} posts. (Total new: {new_count})")

        since_id = next_since_id
        if not since_id:
            print("No next_since_id. Reached end of timeline.")
            break

        if max_pages is not None and page >= max_pages:
            print(f"Reached max_pages ({max_pages}). Stopping.")
            break

        time.sleep(random.uniform(sleep_min, sleep_max))

    print(f"Done. New posts appended: {new_count}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", default="6347862377")
    parser.add_argument("--out", default="data")
    parser.add_argument("--cookie", default=None, help="Cookie string; defaults to env WEIBO_COOKIE.")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--sleep-min", type=float, default=0.8)
    parser.add_argument("--sleep-max", type=float, default=1.8)
    parser.add_argument(
        "--stop-when-seen",
        action="store_true",
        help="If posts.jsonl exists, stop when reaching an existing post ID (good for incremental updates)",
    )
    # Browser arguments
    parser.add_argument(
        "--manual-auth",
        action="store_true",
        help="Open browser window for manual login/verification before scraping",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default: False if manual-auth is not set, otherwise True)",
    )

    args = parser.parse_args()

    out_dir = Path(args.out)
    
    # Logic:
    # 1. If --manual-auth is set, force browser to get new cookie.
    # 2. Else if valid cookie provided (arg or env), use it.
    # 3. Else try auto browser (headless).

    cookie = args.cookie or os.environ.get("WEIBO_COOKIE")
    use_browser = False

    if args.manual_auth:
        print("Manual auth requested. Ignoring provided cookie (if any) and launching browser.")
        use_browser = True
    elif not cookie:
        print("No cookie provided via --cookie or WEIBO_COOKIE.")
        use_browser = True
    
    if use_browser:
        if HAS_PLAYWRIGHT:
            print("Attempting to fetch cookie via Playwright browser...")
            try:
                # If manual_auth is on -> headless=False
                # If not manual_auth -> headless=True (default automation behavior) or respect args.headless
                
                should_be_headless = args.headless
                if args.manual_auth:
                    should_be_headless = False
                
                cookie = get_cookie_from_browser(args.uid, manual_auth=args.manual_auth, headless=should_be_headless)
            except Exception as e:
                print(f"Browser auth failed: {e}")
                print("Tip: Run `playwright install` to install browsers.")
                sys.exit(1)
        else:
            print("Playwright not installed. Cannot use browser automation.")
            print("Install it with: pip install playwright && playwright install")
            print("Or provide a cookie manually.")
            sys.exit(1)
    else:
        print("Using provided cookie string/env var.")

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
