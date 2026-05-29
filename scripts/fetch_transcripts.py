#!/usr/bin/env python3
"""Live fetcher: populate the cache with Trump 2ND-TERM American Presidency
Project spoken-remark DOCUMENT BODIES. Closes the v1 spoken-remark blind spot.

Why this exists
---------------
The originally cached app_*.html were 1st-term searches (person2=200301) -> they
return only 2015-2020 docs, ALL out of scope, so the transcript layer yielded 0
in-scope events. Two fixes here:
  1. Re-run each search with the 2ND-TERM person id (375125 = "Donald J. Trump
     (2nd Term)") -> 2025+ documents.
  2. CRUCIALLY: a search page is only an *index*. The words Trump actually said
     live on each /documents/<slug> page (`.field-docs-content`). So for each
     result we FOLLOW the link and fetch the full transcript BODY.

Writes (under cache/):
  app_<query>.html        2nd-term search listing  (overwrites stale 1st-term)
  appdoc_<slug>.html      full document/transcript body page

Polite + idempotent: every page cached, 0.4s delay, already-cached docs skipped.

Run:  python3 fetch_transcripts.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (ai-investment-system policy-signal research)"}
APP = "https://www.presidency.ucsb.edu"
TRUMP_2ND_TERM = "375125"          # "Donald J. Trump (2nd Term)" on APP
PER_QUERY = 16                     # top docs/query to fetch bodies for
MAX_DOCS = 240                     # global body-fetch cap (logged if hit)

# Theme + high-value entity keyword queries (mirror the generator's themes +
# the names most likely to be spoken in a market-material remark).
QUERIES = [
    "tariffs", "trade deal", "semiconductors", "artificial intelligence",
    "data center", "chips", "oil", "energy", "Federal Reserve", "interest rates",
    "drug prices", "pharmaceutical", "bitcoin", "crypto", "defense", "manufacturing",
    "Nvidia", "Dell", "Apple", "TSMC", "Boeing", "steel",
]


def _cache_path(key: str) -> Path:
    return CACHE / (re.sub(r"[^a-zA-Z0-9_.-]", "_", key)[:120] + ".html")


def get(url: str, key: str, force: bool = False):
    cf = _cache_path(key)
    if cf.exists() and not force:
        return cf.read_text(encoding="utf-8", errors="ignore"), True
    try:
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 200 and r.text:
            cf.write_text(r.text, encoding="utf-8")
            time.sleep(0.4)
            return r.text, False
        print(f"  HTTP {r.status_code} for {url}", file=sys.stderr)
    except Exception as e:
        print(f"  fetch fail {url}: {type(e).__name__}", file=sys.stderr)
    return None, False


def search_docs(query: str):
    """Return ordered, de-duped /documents/<slug> hrefs for a 2nd-term keyword search."""
    url = (f"{APP}/advanced-search?field-keywords={requests.utils.quote(query)}"
           f"&person2={TRUMP_2ND_TERM}&items_per_page=25")
    html, _ = get(url, f"app_{query}", force=True)   # always refresh listing to 2nd-term
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    hrefs, seen = [], set()
    for a in soup.select("td.views-field-title a[href^='/documents/'], "
                         ".views-row a[href^='/documents/']"):
        href = a.get("href", "")
        if re.search(r"guidebook|category-attributes|archive", href):
            continue
        if href not in seen:
            seen.add(href)
            hrefs.append(href)
    return hrefs[:PER_QUERY]


def main():
    print(f"fetching 2nd-term (person {TRUMP_2ND_TERM}) APP transcripts -> {CACHE}")
    all_slugs, seen = [], set()
    for q in QUERIES:
        slugs = search_docs(q)
        print(f"  search {q!r:24} -> {len(slugs)} docs")
        for s in slugs:
            if s not in seen:
                seen.add(s)
                all_slugs.append(s)

    print(f"\nunique documents discovered: {len(all_slugs)}")
    if len(all_slugs) > MAX_DOCS:
        print(f"  CAP: fetching bodies for first {MAX_DOCS} of {len(all_slugs)} "
              f"(remaining {len(all_slugs) - MAX_DOCS} skipped THIS run; re-run to extend)")
        all_slugs = all_slugs[:MAX_DOCS]

    n_ok = n_cached = 0
    for i, slug in enumerate(all_slugs, 1):
        key = "appdoc_" + slug.split("/documents/")[-1]
        body, cached = get(APP + slug, key)
        if body:
            n_ok += 1
            n_cached += int(cached)
        if i % 25 == 0:
            print(f"  bodies {i}/{len(all_slugs)}")
    print(f"\ndone: {n_ok} document bodies in cache "
          f"({n_cached} already cached, {n_ok - n_cached} newly fetched)")


if __name__ == "__main__":
    main()
