#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import subprocess
import sys


def main() -> None:
    uid = os.environ.get("WEIBO_UID", "6347862377")
    out = os.environ.get("WEIBO_OUT", "data")
    docs = os.environ.get("WEIBO_DOCS", "docs")

    subprocess.check_call([sys.executable, "scripts/scrape_weibo.py", "--uid", uid, "--out", out, "--stop-when-seen"])
    subprocess.check_call([sys.executable, "scripts/build_site.py", "--in", out, "--out", docs])


if __name__ == "__main__":
    main()
