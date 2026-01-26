#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from dateutil import parser as date_parser


def _read_posts(jsonl_path: Path) -> List[Dict[str, Any]]:
    posts: List[Dict[str, Any]] = []
    if not jsonl_path.exists():
        return posts
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            posts.append(json.loads(line))
    return posts


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


def _post_html(p: Dict[str, Any]) -> str:
    created = html.escape(_fmt_date(p.get("created_at") or ""))
    source = p.get("source")
    source_txt = ""
    if source:
        # The source field is often HTML, e.g. <a href=...>iPhone</a>
        source_txt = f"<span class='source'> · {source}</span>"

    text_html = p.get("text_html") or ""

    pics = p.get("pics") or []
    imgs = []
    for pic in pics:
        lp = pic.get("local_path")
        if not lp:
            continue
        # Images are under data/images; build copies them to docs/assets/images
        imgs.append(
            f"<a class='img' href='assets/images/{html.escape(lp)}' target='_blank' rel='noreferrer'>"
            f"<img loading='lazy' src='assets/images/{html.escape(lp)}' /></a>"
        )

    img_html = ""
    if imgs:
        img_html = "<div class='imgs'>" + "".join(imgs) + "</div>"

    return (
        "<article class='post'>"
        f"<div class='meta'><time>{created}</time>{source_txt}</div>"
        f"<div class='text'>{text_html}</div>"
        f"{img_html}"
        "</article>"
    )


def build(in_dir: Path, out_dir: Path, title: str) -> None:
    posts = _read_posts(in_dir / "posts.jsonl")
    posts = _sort_posts(posts)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    # copy images
    src_images = in_dir / "images"
    dst_images = out_dir / "assets" / "images"
    if src_images.exists():
        # Python 3.8+ supports dirs_exist_ok
        import shutil

        dst_images.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_images, dst_images, dirs_exist_ok=True)

    rendered = "\n".join(_post_html(p) for p in posts)
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    page = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; --bg:#0b0c10; --card:#12141a; --text:#e8e8ea; --muted:#9aa0a6; --link:#7aa2ff; --border:#242833; }}
    @media (prefers-color-scheme: light) {{
      :root {{ --bg:#f7f7fb; --card:#ffffff; --text:#111827; --muted:#6b7280; --link:#2563eb; --border:#e5e7eb; }}
    }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); }}
    a {{ color:var(--link); }}
    .wrap {{ max-width: 880px; margin: 0 auto; padding: 24px 16px 60px; }}
    header {{ display:flex; justify-content:space-between; gap:16px; align-items:baseline; margin-bottom:16px; }}
    h1 {{ font-size: 20px; margin: 0; }}
    .sub {{ color: var(--muted); font-size: 12px; }}
    .post {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 14px; margin: 12px 0; }}
    .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
    .text {{ font-size: 15px; line-height: 1.6; word-break: break-word; }}
    .text img {{ max-height: 1em; vertical-align: text-bottom; }}
    .imgs {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }}
    .imgs img {{ width:100%; height: 180px; object-fit: cover; border-radius: 10px; border: 1px solid var(--border); background: #000; }}
    @media (max-width: 640px) {{ .imgs {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .imgs img {{ height: 160px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>{html.escape(title)}</h1>
      <div class="sub">Posts: {len(posts)} · Generated: {generated_at}</div>
    </header>
    {rendered}
  </div>
</body>
</html>
"""

    (out_dir / "index.html").write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="in_dir", default="data")
    parser.add_argument("--out", dest="out_dir", default="docs")
    parser.add_argument("--title", default="Yin Wang Weibo Archive")
    args = parser.parse_args()

    build(Path(args.in_dir), Path(args.out_dir), args.title)


if __name__ == "__main__":
    main()
