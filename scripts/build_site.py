#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import html
import json
import re
import shutil
import urllib.parse
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

def _get_year_month(iso: str) -> Tuple[str, str]:
    try:
        dt = date_parser.parse(iso.replace("Z", "+00:00"))
        return str(dt.year), f"{dt.year}-{dt.month:02d}"
    except Exception:
        return "Unknown", "Unknown"


def _clean_weibo_links(html_content: str) -> str:
    """
    Replaces https://weibo.cn/sinaurl?u=... with the decoded direct URL.
    """
    if not html_content:
        return ""
    
    def replacer(match):
        encoded_url = match.group(1)
        try:
            return urllib.parse.unquote(encoded_url)
        except Exception:
            return encoded_url

    # Regex looks for the u= parameter value until the next quote or ampersand
    # Pattern: https://weibo.cn/sinaurl?u=([^"&' <]+)
    pattern = r"https?://weibo\.cn/sinaurl\?u=([^\"'&]+)"
    return re.sub(pattern, replacer, html_content)


def _render_images(pics: List[Dict[str, Any]]) -> str:
    if not pics:
        return ""
    
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
            continue
            
        src = f"assets/images/{html.escape(lp)}"
        # Use data-src for lightbox
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
    created_raw = p.get("created_at") or ""
    created_str = html.escape(_fmt_date(created_raw))
    year, month = _get_year_month(created_raw)
    
    source = p.get("source")
    source_txt = ""
    if source:
        source_txt = f" · {html.escape(source)}"
    
        source_txt = f" · {html.escape(source)}"
    
    text_html = p.get("text_html") or ""
    text_html = _clean_weibo_links(text_html)
    
    pics_html = _render_images(p.get("pics") or [])
    
    retweet_html = ""
    if p.get("retweeted_status"):
        rt = p["retweeted_status"]
        rt_user = rt.get("user") or {}
        rt_user_name = rt_user.get("screen_name") or "Unknown"
        rt_user_name = rt_user.get("screen_name") or "Unknown"
        rt_text = rt.get("text") or ""
        rt_text = _clean_weibo_links(rt_text)
        retweet_html = (
            f"<div class='retweet'>"
            f"<div class='rt-user'>@{html.escape(rt_user_name)}</div>"
            f"<div class='rt-text'>{rt_text}</div>"
            f"</div>"
        )

        r"https?://weibo\.cn/sinaurl\?u=([^\"'&]+)"
    
    return (
        f"<article class='post' id='post-{pid}' data-year='{year}' data-month='{month}'>"
        f"<div class='meta'>"
        f"  <span class='date'><a href='#post-{pid}' class='permalink'>{created_str}</a></span>"
        f"  <span class='source'>{source_txt}</span>"
        f"</div>"
        f"<div class='content'>"
        f"  <div class='text-block is-collapsed'>"
        f"    <div class='text'>{text_html}</div>"
        f"    {retweet_html}"
        f"  </div>"
        f"  <button class='expand-toggle' type='button' hidden aria-expanded='false'>展开</button>"
        f"  {pics_html}"
        f"</div>"
        f"</article>"
    )


def _build_sidebar(posts: List[Dict[str, Any]]) -> str:
    # Group by Year -> Month
    tree = defaultdict(set)
    for p in posts:
        y, m = _get_year_month(p.get("created_at") or "")
        if y != "Unknown":
            tree[y].add(m)
    
    # Sort descending
    years = sorted(tree.keys(), reverse=True)
    
    html_parts = []
    html_parts.append("<div class='nav-group'>")
    html_parts.append(f"<div class='nav-item active' data-filter='all'>All Posts <span class='count'>({len(posts)})</span></div>")
    html_parts.append("</div>")

    for y in years:
        months = sorted(list(tree[y]), reverse=True)
        html_parts.append("<div class='nav-group'>")
        html_parts.append(f"<div class='nav-year'>{y}</div>")
        for m in months:
            # Count posts in this month
            c = sum(1 for p in posts if _get_year_month(p.get("created_at") or "")[1] == m)
            label = m.split("-")[1] # Just the month part "05"
            # Format nicely? "2025-05" -> "05" works.
            html_parts.append(f"<div class='nav-item' data-filter='{m}'>{y}-{label} <span class='count'>({c})</span></div>")
        html_parts.append("</div>")
    
    return "\n".join(html_parts)


def build(in_dir: Path, out_dir: Path, title: str) -> None:
    posts = _read_posts_deduplicated(in_dir / "posts.jsonl")
    posts = _sort_posts(posts)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    src_images = in_dir / "images"
    dst_images = out_dir / "assets" / "images"
    if src_images.exists():
        dst_images.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_images, dst_images, dirs_exist_ok=True)

    rendered_posts = "\n".join(_post_html(p) for p in posts)
    sidebar_html = _build_sidebar(posts)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    
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
      --accent: #eb552d;
      --link: #4b89dc;
      --border: #eaeaea;
      --shadow: 0 1px 3px rgba(0,0,0,0.05);
      --radius: 12px;
      --sidebar-width: 240px;
            --collapse-max-height: 220px;
            --collapse-fade-height: 70px;
            --pics-max-width: 520px;
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
      display: flex;
      min-height: 100vh;
    }}
    
    /* SIDEBAR */
    .sidebar {{
      width: var(--sidebar-width);
      background: var(--card-bg);
      border-right: 1px solid var(--border);
      height: 100vh;
      position: sticky;
      top: 0;
      overflow-y: auto;
      padding: 20px;
      box-sizing: border-box;
      flex-shrink: 0;
    }}
    
    /* MAIN */
    .main-content {{
      flex-grow: 1;
      padding: 40px;
      max-width: 800px; /* Limit content width */
      margin: 0 auto;
    }}

    /* Header in Sidebar */
    .brand {{
      margin-bottom: 30px;
    }}
    .brand h1 {{
      font-size: 20px;
      margin: 0 0 4px 0;
      font-weight: 700;
    }}
    .brand .stats {{
      font-size: 12px;
      color: var(--text-sub);
    }}
    
    .search-sub {{
        width: 100%;
        padding: 8px 12px;
        border-radius: 6px;
        border: 1px solid var(--border);
        background: var(--bg);
        color: var(--text-main);
        font-size: 14px;
        outline: none;
        margin-bottom: 20px;
    }}
    .search-sub:focus {{ border-color: var(--accent); }}

    /* Nav Items */
    .nav-group {{ margin-bottom: 20px; }}
    .nav-year {{ font-weight: 700; font-size: 14px; margin-bottom: 8px; color: var(--text-main); }}
    .nav-item {{
        padding: 6px 10px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 14px;
        color: var(--text-sub);
        display: flex;
        justify-content: space-between;
    }}
    .nav-item:hover {{ background: var(--bg); color: var(--text-main); }}
    .nav-item.active {{ background: var(--accent); color: white; }}
    .nav-item .count {{ opacity: 0.7; font-size: 12px; }}

    /* POST CARD */
    .post {{
      background: var(--card-bg);
      border-radius: var(--radius);
      padding: 24px;
      margin-bottom: 24px;
      box-shadow: var(--shadow);
    }}
    .meta {{ display: flex; gap: 8px; margin-bottom: 12px; font-size: 13px; color: var(--text-sub); }}
    .date {{ font-weight: 500; }}
    .text {{ font-size: 16px; margin-bottom: 12px; overflow-wrap: break-word; }}

        /* COLLAPSE / EXPAND */
        .text-block {{ position: relative; }}
        .text-block.is-collapsed {{ max-height: var(--collapse-max-height); overflow: hidden; }}
        .text-block.is-collapsed::after {{
            content: "";
            position: absolute;
            left: 0;
            right: 0;
            bottom: 0;
            height: var(--collapse-fade-height);
            background: linear-gradient(to bottom, rgba(0,0,0,0), var(--card-bg));
            pointer-events: none;
        }}
        .expand-toggle {{
            display: inline-block;
            margin: 6px 0 0;
            padding: 0;
            border: 0;
            background: transparent;
            color: var(--link);
            font-size: 14px;
            cursor: pointer;
        }}
        .expand-toggle[hidden] {{ display: none !important; }}
        .expand-toggle:hover {{ text-decoration: underline; }}
    
    .retweet {{
      background: var(--bg);
      padding: 12px 16px;
      border-radius: 8px;
      margin-bottom: 12px;
      font-size: 14px;
      color: var(--text-sub);
    }}
    .rt-user {{ font-weight: 600; margin-bottom: 4px; color: var(--text-main); }}

    /* IMAGES */
    .pics {{ display: grid; gap: 6px; margin-top: 12px; max-width: var(--pics-max-width); }}
    .img-item {{ display: block; position: relative; border-radius: 8px; background: #f0f0f0; overflow: hidden; cursor: zoom-in; }}
    .img-item img {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
    .pics-1 {{ max-width: 60%; grid-template-columns: 1fr; }} 
    .pics-1 .img-item {{ max-height: 500px; }}
    .pics-1 .img-item img {{ height: auto; }}
    .pics-2 {{ grid-template-columns: repeat(2, 1fr); }} .pics-2 .img-item {{ aspect-ratio: 1; }}
    .pics-3 {{ grid-template-columns: repeat(3, 1fr); }} .pics-3 .img-item {{ aspect-ratio: 1; }}
    
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    
    /* LIGHTBOX */
    #lightbox {{
        display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%;
        background-color: rgba(0,0,0,0.9); align-items: center; justify-content: center;
    }}
    #lightbox img {{ max-width: 90%; max-height: 90%; box-shadow: 0 0 20px rgba(0,0,0,0.5); border-radius: 4px; }}
    #lightbox.active {{ display: flex; }}

    /* MOBILE: App-like Experience */
    @media (max-width: 768px) {{
                :root {{
                    --collapse-max-height: 260px;
                    --collapse-fade-height: 80px;
                    --pics-max-width: 100%;
                }}
        body {{ 
            flex-direction: column; 
            background: var(--card-bg); /* Seamless background */
        }}
        
        /* Header: Not sticky, clean background */
        .sidebar {{ 
            width: 100%; 
            height: auto; 
            position: relative; /* Let it scroll away */
            border-right: none; 
            border-bottom: 1px solid var(--border); 
            padding: 16px 20px; 
            background: var(--card-bg); 
            z-index: 10;
        }}

        .brand {{ 
            margin-bottom: 12px; 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
        }}
        .brand h1 {{ font-size: 20px; margin: 0; }}
        
        .search-sub {{ 
            margin-bottom: 16px; 
            background: var(--bg);
            border: none;
            padding: 10px 14px;
            font-size: 15px;
        }}

        /* Navigation: Chips */
        #navMenu {{
            display: flex;
            overflow-x: auto;
            gap: 10px;
            padding-bottom: 4px;
            -webkit-overflow-scrolling: touch;
            white-space: nowrap;
            scrollbar-width: none; 
            -ms-overflow-style: none;
        }}
        #navMenu::-webkit-scrollbar {{ display: none; }}
        
        .nav-group {{ 
            display: flex; 
            flex-wrap: nowrap;
            gap: 10px; 
            margin: 0;
            flex-shrink: 0;
            align-items: center;
        }}
        
        .nav-year {{ display: none; }} /* Hide year, implied by context or redundant */
        
        .nav-item {{
            font-size: 14px;
            padding: 8px 16px;
            border-radius: 20px; /* Pill shape */
            background: var(--bg);
            color: var(--text-main);
            border: none;
            font-weight: 500;
        }}
        .nav-item.active {{
            background: var(--accent);
            color: white;
            box-shadow: 0 4px 12px rgba(235, 85, 45, 0.25);
        }}
        .nav-item .count {{
            font-size: 11px;
            opacity: 0.8;
            margin-left: 4px;
        }}

        /* Feed: Full width, Clean */
        .main-content {{ 
            padding: 0; 
            max-width: 100%; 
        }}
        
        .post {{ 
            box-shadow: none;
            border-radius: 0;
            margin-bottom: 0;
            border-bottom: 1px solid var(--border);
            padding: 24px 20px;
        }}
        .post:last-child {{ border-bottom: none; }}
        
        /* Typography adjustments for mobile */
        .text {{ font-size: 17px; line-height: 1.7; }}
        .meta {{ margin-bottom: 14px; }}
        
        .pics-1 {{ max-width: 100%; }}
    }}
  </style>
</head>
<body>

  <aside class="sidebar">
    <div class="brand">
      <h1>{html.escape(title)}</h1>
      <div class="stats">{len(posts)} Posts</div>
    </div>
    
    <input type="text" class="search-sub" id="searchInput" placeholder="Search..." />
    
    <div id="navMenu">
        {sidebar_html}
    </div>
  </aside>

  <main class="main-content">
    <div id="postsList">
      {rendered_posts}
    </div>
    
    <div style="text-align:center; color:var(--text-sub); font-size:12px; margin-top:40px;">
        Generated by yinwang-weibo-archiver at {generated_at}
    </div>
  </main>

  <!-- Lightbox -->
  <div id="lightbox">
      <img id="lightboxImg" src="" />
  </div>

  <script>
    const searchInput = document.getElementById('searchInput');
    const posts = document.querySelectorAll('.post');
    const navItems = document.querySelectorAll('.nav-item');
    const lightbox = document.getElementById('lightbox');
    const lightboxImg = document.getElementById('lightboxImg');
    
    // State
    const urlParams = new URLSearchParams(window.location.search);
    let currentFilter = urlParams.get('date') || 'all';
    let currentSearch = urlParams.get('q') || '';

    // Init
    function init() {{
        // Restore search input
        if (currentSearch) {{
            searchInput.value = currentSearch;
        }}

        // Restore active nav
        if (currentFilter) {{
            navItems.forEach(n => n.classList.remove('active'));
            // Find the item with date-filter == currentFilter
            // Note: 'all' is a special case
            const target = Array.from(navItems).find(n => n.getAttribute('data-filter') === currentFilter);
            if (target) {{
                target.classList.add('active');
            }} else {{
                // Fallback to all if not found
                document.querySelector('[data-filter="all"]').classList.add('active');
            }}
        }}

        // Apply filters
        filterPosts(currentFilter, currentSearch);

        // Handle Hash (Anchor)
        const hash = window.location.hash;
        if (hash) {{
            const targetId = hash.substring(1); // remove #
            const targetEl = document.getElementById(targetId);
            if (targetEl) {{
                if (targetEl.style.display === 'none') {{
                    // Force switch to 'all' to ensure visibility
                    console.log("Deep linked post hidden by filter, switching to 'all'.");
                    updateState('all', ''); // Reset filters
                    
                    // Update UI
                    navItems.forEach(n => n.classList.remove('active'));
                    document.querySelector('[data-filter="all"]').classList.add('active');
                    searchInput.value = '';
                }}
                
                setTimeout(() => {{
                    targetEl.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                    targetEl.classList.add('highlight');
                }}, 500);
            }}
        }}
    }}

    // 1. Search Logic
    searchInput.addEventListener('input', (e) => {{
        const term = e.target.value.toLowerCase().trim();
        updateState(currentFilter, term);
    }});
    
    // 2. Nav Logic
    navItems.forEach(item => {{
        item.addEventListener('click', () => {{
             // Clean active classes
            navItems.forEach(n => n.classList.remove('active'));
            item.classList.add('active');
            
            const newFilter = item.getAttribute('data-filter');
            updateState(newFilter, searchInput.value.toLowerCase().trim());
            
            // Scroll top
            window.scrollTo(0, 0);
        }});
    }});

    function updateState(filter, search) {{
        currentFilter = filter;
        currentSearch = search;
        
        filterPosts(filter, search);
        
        // Update URL
        const params = new URLSearchParams();
        if (filter !== 'all') params.set('date', filter);
        if (search) params.set('q', search);
        
        const newUrl = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
        window.history.replaceState({{}}, '', newUrl);
    }}

    function filterPosts(filter, searchTerm) {{
        posts.forEach(post => {{
            const postYear = post.getAttribute('data-year');
            const postMonth = post.getAttribute('data-month');
            const text = post.innerText.toLowerCase();
            
            let matchesFilter = (filter === 'all') || (postMonth === filter);
            let matchesSearch = !searchTerm || text.includes(searchTerm);
            
            if (matchesFilter && matchesSearch) {{
                post.style.display = 'block';
            }} else {{
                post.style.display = 'none';
            }}
        }});
    }}

    // 3. Collapse / Expand Logic
    function setupCollapsibles() {{
        document.querySelectorAll('.post').forEach(post => {{
            const block = post.querySelector('.text-block');
            const btn = post.querySelector('.expand-toggle');
            if (!block || !btn) return;

            if (!btn.dataset.bound) {{
                btn.addEventListener('click', () => {{
                    const isCollapsed = block.classList.toggle('is-collapsed');
                    btn.textContent = isCollapsed ? '展开' : '收起';
                    btn.setAttribute('aria-expanded', String(!isCollapsed));
                }});
                btn.dataset.bound = '1';
            }}

            // Default: collapsed; only show toggle if overflow exists
            block.classList.add('is-collapsed');
            btn.textContent = '展开';
            btn.setAttribute('aria-expanded', 'false');

            const needsToggle = block.scrollHeight > block.clientHeight + 4;
            if (needsToggle) {{
                btn.hidden = false;
            }} else {{
                btn.hidden = true;
                block.classList.remove('is-collapsed');
            }}
        }});
    }}
    
    // 4. Lightbox Logic
    document.querySelectorAll('.img-item').forEach(link => {{
        link.addEventListener('click', (e) => {{
            e.preventDefault();
            const src = link.getAttribute('href');
            lightboxImg.src = src;
            lightbox.classList.add('active');
        }});
    }});
    
    lightbox.addEventListener('click', () => {{
        lightbox.classList.remove('active');
    }});

        // Run
        init();
        setupCollapsibles();
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
