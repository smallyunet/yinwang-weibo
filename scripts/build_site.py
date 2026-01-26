#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import html
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dateutil import parser as date_parser


def _read_posts_deduplicated(jsonl_path: Path) -> List[Dict[str, Any]]:
    """
    Reads posts from JSONL and removes duplicates based on 'id'.
    Later entries override earlier ones.
    """
    posts_map: Dict[str, Dict[str, Any]] = {}
    if not jsonl_path.exists():
        return []
    
    total_lines = 0
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
                pid = str(p.get("id"))
                if pid:
                    posts_map[pid] = p
            except Exception:
                continue
    
    unique_posts = list(posts_map.values())
    print(f"Read {total_lines} lines, found {len(unique_posts)} unique posts.")
    return unique_posts


def _sort_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(p: Dict[str, Any]) -> float:
        t = p.get("created_at") or ""
        try:
            dt = date_parser.parse(t.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0

    # Newest first
    return sorted(posts, key=key, reverse=True)


def _fmt_date(iso: str) -> str:
    try:
        dt = date_parser.parse(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def _render_images(pics: List[Dict[str, Any]]) -> str:
    if not pics:
        return ""
    
    # Logic for image grid structure
    count = len(pics)
    grid_class = "pics-1"
    if count == 1:
        grid_class = "pics-1"
    elif count == 2 or count == 4:
        grid_class = "pics-2"
    else:
        grid_class = "pics-3"

    imgs_html = []
    for pic in pics:
        lp = pic.get("local_path")
        if not lp:
            # Fallback to remote URL if local not found?
            # Ideally we only use local.
            continue
            
        src = f"assets/images/{html.escape(lp)}"
        # Use a simple link for now, could be lightbox
        img_tag = (
            f"<a class='img-item' href='{src}' target='_blank' rel='noreferrer'>"
            f"<img loading='lazy' src='{src}' />"
            "</a>"
        )
        imgs_html.append(img_tag)

    if not imgs_html:
        return ""

    return f"<div class='pics {grid_class}'>{''.join(imgs_html)}</div>"


def _post_html(p: Dict[str, Any]) -> str:
    pid = p.get("id")
    created = html.escape(_fmt_date(p.get("created_at") or ""))
    source = p.get("source")
    source_txt = ""
    if source:
        source_txt = f" · {html.escape(source)}" 
        # Source often contains HTML in raw data, but let's be careful. 
        # If raw source has HTML tags, we might want to strip them or trust them?
        # The scraper passes 'source' usually roughly clean or with simple tags. 
        # Let's strip tags for safety/uniformity if needed, but existing code used it directly.
        # Actually scrape_weibo.py passes raw source which might be "iPhone 13". 
        # Sometimes it's link text. Let's strict escape to be safe.
    
    text_html = p.get("text_html") or ""
    
    # Process images
    pics_html = _render_images(p.get("pics") or [])
    
    # Handle Retweet
    retweet_html = ""
    if p.get("retweeted_status"):
        rt = p["retweeted_status"]
        rt_user = rt.get("user") or {}
        rt_user_name = rt_user.get("screen_name") or "Unknown"
        rt_text = rt.get("text") or ""
        # RT images
        rt_pics = _render_images(p.get("pics") or []) # Wait, scraper logic puts RT pics in main pics?
        # Actually standard Weibo logic: RT pics are in retweeted_status['pics'] usually.
        # But our scraper flat structure might vary. 
        # Checking scraper: it extracts pics from mblog. 
        # If it's a retweet, mblog['pics'] usually contains the RT images?
        # Or mblog['retweeted_status']['pics']?
        # The scraper `_extract_pics` takes `mblog`. 
        # If `mblog` has `pics`, it uses them. 
        # Let's assume the `pics` field in our `WeiboPost` covers the images to be shown.
        # If it's a pure retweet, usually the main post has no pics, the RT has pics.
        # Our `WeiboPost` definition has `pics` and `retweeted_status`. 
        # If `is_retweet` is True, usually the pics belong to the RT content. 
        # But `scrape_weibo.py` logic: `_extract_pics(mblog)`. 
        # If it is a retweet, the outer mblog might NOT have pics, but `retweeted_status` has.
        # Let's check scraper behavior. Scraper runs `_extract_pics(mblog)`.
        # Unless we updated scraper to dig into RT, we might miss RT pics if they are only inside `retweeted_status`.
        # However, looking at `WeiboPost` dataclass, it serves the flattened view. 
        # Let's stick to using `p.get("pics")` for now.
        
        retweet_html = (
            f"<div class='retweet'>"
            f"<div class='rt-user'>@{html.escape(rt_user_name)}</div>"
            f"<div class='rt-text'>{rt_text}</div>"
            f"</div>"
        )

    return (
        f"<article class='post' id='post-{pid}'>"
        f"<div class='meta'>"
        f"  <span class='date'>{created}</span>"
        f"  <span class='source'>{source_txt}</span>"
        f"</div>"
        f"<div class='content'>"
        f"  <div class='text'>{text_html}</div>"
        f"  {retweet_html}"
        f"  {pics_html}"
        f"</div>"
        f"</article>"
    )


def build(in_dir: Path, out_dir: Path, title: str) -> None:
    posts = _read_posts_deduplicated(in_dir / "posts.jsonl")
    posts = _sort_posts(posts)

    if out_dir.exists():
        # Maybe clean it? Or just overwrite.
        pass
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    # Copy images
    src_images = in_dir / "images"
    dst_images = out_dir / "assets" / "images"
    if src_images.exists():
        dst_images.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_images, dst_images, dirs_exist_ok=True)

    rendered_posts = "\n".join(_post_html(p) for p in posts)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Premium CSS & Layout
    page_content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f5f7f9;
      --card-bg: #ffffff;
      --text-main: #1f2937;
      --text-sub: #6b7280;
      --accent: #eb552d; /* Weibo Orange/Red */
      --link: #4b89dc;
      --border: #eaeaea;
      --shadow: 0 1px 3px rgba(0,0,0,0.05);
      --radius: 12px;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111;
        --card-bg: #1e1e1e;
        --text-main: #e5e7eb;
        --text-sub: #9ca3af;
        --border: #333;
        --shadow: none;
      }}
    }}
    
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }}

    .container {{
      max-width: 720px;
      margin: 0 auto;
      padding: 40px 16px;
    }}

    header {{
      margin-bottom: 40px;
      text-align: center;
    }}
    h1 {{
      margin: 0 0 8px 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.5px;
    }}
    .stats {{
      font-size: 13px;
      color: var(--text-sub);
    }}
    
    .search-box {{
      margin: 20px auto 0;
      max-width: 400px;
      position: relative;
    }}
    .search-sub {{
        width: 100%;
        padding: 10px 16px;
        border-radius: 99px;
        border: 1px solid var(--border);
        background: var(--card-bg);
        color: var(--text-main);
        font-size: 14px;
        outline: none;
        box-sizing: border-box;
        transition: all 0.2s;
    }}
    .search-sub:focus {{
        border-color: var(--accent);
        box-shadow: 0 0 0 3px rgba(235, 85, 45, 0.1);
    }}

    /* POST CARD */
    .post {{
      background: var(--card-bg);
      border-radius: var(--radius);
      padding: 24px;
      margin-bottom: 24px;
      box-shadow: var(--shadow);
      transition: transform 0.2s;
    }}
    .post:hover {{
      /* transform: translateY(-2px); subtle lift */
    }}

    .meta {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
      font-size: 13px;
      color: var(--text-sub);
    }}
    .date {{
      font-weight: 500;
    }}

    .text {{
      font-size: 16px;
      margin-bottom: 12px;
      overflow-wrap: break-word;
    }}
    
    /* Retweet block */
    .retweet {{
      background: var(--bg);
      padding: 12px 16px;
      border-radius: 8px;
      margin-bottom: 12px;
      font-size: 14px;
      color: var(--text-sub);
    }}
    .rt-user {{
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--text-main);
    }}

    /* IMAGES GRID */
    .pics {{
      display: grid;
      gap: 6px;
      margin-top: 12px;
    }}
    .img-item {{
      display: block;
      position: relative;
      overflow: hidden;
      border-radius: 8px;
      background: #f0f0f0;
      cursor: zoom-in;
    }}
    .img-item img {{
      display: block;
      width: 100%;
      height: 100%;
      object-fit: cover;
      transition: opacity 0.3s;
    }}
    
    /* Grid variants */
    .pics-1 {{
      grid-template-columns: 1fr;
      max-width: 60%; 
    }}
    .pics-1 .img-item {{
      aspect-ratio: auto;
      max-height: 500px;
    }}
    .pics-1 .img-item img {{
        height: auto;
        max-height: 500px;
    }}

    .pics-2 {{
        grid-template-columns: repeat(2, 1fr);
    }}
    .pics-2 .img-item {{
        aspect-ratio: 1; 
    }}

    .pics-3 {{
        grid-template-columns: repeat(3, 1fr);
    }}
    .pics-3 .img-item {{
        aspect-ratio: 1;
    }}
    
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    
    @media (max-width: 600px) {{
        .container {{ padding: 20px 12px; }}
        .pics-1 {{ max-width: 100%; }}
        .post {{ padding: 16px; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>{html.escape(title)}</h1>
      <div class="stats">
        {len(posts)} Posts · Generated {generated_at}
      </div>
      <div class="search-box">
        <input type="text" class="search-sub" id="searchInput" placeholder="Search content or date..." />
      </div>
    </header>

    <div id="postsList">
      {rendered_posts}
    </div>
    
  </div>

  <script>
    const searchInput = document.getElementById('searchInput');
    const postsContainer = document.getElementById('postsList');
    const posts = document.querySelectorAll('.post');

    searchInput.addEventListener('input', (e) => {{
      const term = e.target.value.toLowerCase().trim();
      
      posts.forEach(post => {{
        const text = post.innerText.toLowerCase();
        if (text.includes(term)) {{
          post.style.display = 'block';
        }} else {{
          post.style.display = 'none';
        }}
      }});
    }});
  </script>
</body>
</html>
"""
    
    (out_dir / "index.html").write_text(page_content, encoding="utf-8")
    print(f"Site built at {out_dir}/index.html")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_dir", default="data")
    parser.add_argument("--out", dest="out_dir", default="docs")
    parser.add_argument("--title", default="Yin Wang Weibo Archive")
    args = parser.parse_args()

    build(Path(args.in_dir), Path(args.out_dir), args.title)


if __name__ == "__main__":
    main()
