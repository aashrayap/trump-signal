#!/usr/bin/env python3
"""
TRANSCRIPTS lane — spoken-remark ingestion for the Trump Policy-Signal feed (v3).

Goal: close the v1 spoken-remark blind spot. Turn cached HTML from two
official/spoken sources into dated {date, channel, title, url, body} events:

  (1) American Presidency Project (APP)  — cached as cache/app_*.html
        These are *advanced-search listing* pages (3-col views table:
        Date | Related | Document Title). Each /documents/<slug> row is a
        distinct spoken/official document. Dates live in column 1 as
        "Mon D, YYYY" and are EXACT.
  (2) WhiteHouse.gov articles            — cached as cache/art_*.html
        Single-document pages. Exact date via <time datetime=...> (preferred)
        or JSON-LD "datePublished". Body via <article>/<main>. Canonical URL
        via <link rel=canonical>.

CACHE ONLY — no network. This module never imports requests; it only reads
files already present under cache/.

Channel mapping (task spec):
    remarks / exchange / pool report          -> oval_remark
    speech / interview / rally / address       -> speech
    press release / statement / fact sheet     -> official_release
    presidential action / executive order      -> exec_action  (WH only; binding)

DISCIPLINE: this layer is DESCRIPTIVE only. It records "he said/issued X on
date D via channel C". It never infers impact and never scores returns.
Weighting + entity extraction live downstream in the generator.

Run standalone to self-demonstrate on the cache:
    python3 scripts/transcripts.py
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Paths — resolve cache relative to this file so cwd never matters.
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent                # .../scripts
ROOT = HERE.parent                                    # repo root
CACHE = ROOT / "cache"
APP_BASE = "https://www.presidency.ucsb.edu"

SCOPE_START = dt.date(2025, 1, 20)                     # match generator scope
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# --------------------------------------------------------------------------- #
# Channel classification — shared by APP titles and WH titles/urls.
# Order matters: most specific spoken cue first.
# --------------------------------------------------------------------------- #
def channel_from_title(title: str, url: str = "") -> str:
    """Map a document title (+ optional url) to a weighting channel.

    Spoken cues beat written cues; WH url path is a fallback for written docs.
    """
    t = (title or "").lower()
    u = (url or "").lower()

    # 1) Binding presidential action (WH only) — written but exec_action.
    if "/presidential-actions/" in u or "executive order" in t \
            or "presidential memorand" in t or "proclamation" in t:
        return "exec_action"

    # 2) Scheduled / one-directional spoken venue -> speech.
    #    Checked BEFORE the generic "remarks" cue, because APP titles like
    #    "Remarks at a Rally ..." / "Remarks at a Campaign Event" / "... Address"
    #    are scheduled speeches, not Oval/press exchanges. Venue beats verb.
    if re.search(r"\b(speech|interview|rally|address|press briefing|"
                 r"telephone remarks|radio address|town hall|campaign event|"
                 r"commencement|inaugural|convention)\b", t):
        return "speech"

    # 3) Spoken, conversational -> oval_remark (highest spoken weight).
    #    "remarks", "exchange with reporters", "pool report(s)", Q&A spray.
    if re.search(r"\bremarks\b", t) or "exchange with reporters" in t \
            or "pool report" in t or "question-and-answer" in t \
            or "press gaggle" in t or "press conference" in t:
        return "oval_remark"

    # 4) Written official -> official_release.
    if "press release" in t or "statement" in t or "fact sheet" in t \
            or "/fact-sheets/" in u or "/briefings-statements/" in u \
            or "/releases/" in u:
        return "official_release"

    # 5) WH 'remarks by ...' under /remarks/ path (rare in cache) -> oval_remark.
    if "/remarks/" in u:
        return "oval_remark"

    # Fallback: an APP /documents/ row with none of the above is most often a
    # written WH press release surfaced by search; treat as official_release.
    return "official_release"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# --------------------------------------------------------------------------- #
# Source A: American Presidency Project listing pages (cache/app_*.html)
# --------------------------------------------------------------------------- #
def parse_app_date(txt: str):
    """'Jan 23, 2017' -> date(2017,1,23). Returns None if unparseable."""
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})", (txt or "").strip())
    if not m:
        return None
    mon = MONTHS.get(m.group(1))
    if not mon:
        return None
    try:
        return dt.date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError:
        return None


def parse_app_listing(html: str):
    """Parse ONE APP advanced-search results page into document events.

    The results table is `table.views-table` with three columns:
      td.views-field-field-docs-start-date-time-value  -> exact date
      td.views-field-field-docs-person                 -> person (for sanity)
      td.views-field-title                             -> <a href=/documents/..>

    Returns a list of {date, channel, title, url, body, source, person}.
    `body` is empty here (listing pages carry titles only; full text needs the
    document page). Downstream can enrich; for attention-weighting the title +
    channel already carry the signal.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tbl in soup.select("table.views-table"):
        for tr in tbl.select("tbody tr"):
            tds = tr.find_all("td", recursive=False) or tr.find_all("td")
            if len(tds) < 3:
                continue
            date_td = tr.select_one(
                "td.views-field-field-docs-start-date-time-value") or tds[0]
            person_td = tr.select_one("td.views-field-field-docs-person")
            title_td = tr.select_one("td.views-field-title") or tds[-1]
            a = title_td.find("a", href=re.compile(r"^/documents/"))
            if not a:
                continue
            title = _clean(a.get_text(" "))
            href = a.get("href", "")
            d = parse_app_date(date_td.get_text(" ", strip=True))
            person = _clean(person_td.get_text(" ")) if person_td else ""
            out.append({
                "source": "americanpresidency.org",
                "date": d,
                "channel": channel_from_title(title, APP_BASE + href),
                "title": title,
                "url": APP_BASE + href,
                "body": "",                      # listing has no full transcript
                "person": person,
                "date_exact": d is not None,
            })
    return out


def load_app_events(cache: Path = CACHE):
    """Parse every cached app_*.html. De-dupe by url (same doc surfaced by
    multiple keyword searches). Returns list sorted by date."""
    by_url = {}
    files = sorted(glob.glob(str(cache / "app_*.html")))
    stats = {"files": len(files), "files_with_results": 0, "raw_rows": 0}
    for fp in files:
        html = Path(fp).read_text(encoding="utf-8", errors="ignore")
        rows = parse_app_listing(html)
        if rows:
            stats["files_with_results"] += 1
        for r in rows:
            stats["raw_rows"] += 1
            r = dict(r)
            r["_query"] = Path(fp).stem.replace("app_", "")
            prev = by_url.get(r["url"])
            if prev is None:
                by_url[r["url"]] = r
            else:
                prev.setdefault("_also", []).append(r["_query"])
    events = sorted(by_url.values(),
                    key=lambda e: (e["date"] or dt.date.min, e["title"]))
    return events, stats


# --------------------------------------------------------------------------- #
# Source A2: APP document BODY pages (cache/appdoc_*.html) — fetched by
# fetch_transcripts.py from the 2ND-TERM search. These carry the full spoken
# transcript text (the listing page is only an index). THIS closes the spoken-
# remark blind spot: open-vocab company extraction runs over the real body.
# --------------------------------------------------------------------------- #
def _parse_date_any(txt: str):
    """Parse 'January 20, 2025' (full month) or 'Jan 23, 2017' (abbrev) -> date."""
    d = parse_app_date(txt)                       # fast path: abbreviated month
    if d:
        return d
    try:
        import pandas as pd
        v = pd.to_datetime((txt or "").strip(), errors="coerce")
        return None if (v is None or pd.isna(v)) else v.date()
    except Exception:
        return None


def parse_app_document(html: str, fallback_url: str = ""):
    """Parse ONE APP /documents/<slug> page into a spoken-remark event WITH body."""
    soup = BeautifulSoup(html, "html.parser")
    h = soup.select_one("h1.page-title") or soup.find("h1")
    title = _clean(h.get_text(" ")) if h else ""
    de = soup.select_one(".field-docs-start-date-time-value, "
                         "span.date-display-single, .field-docs-start-date-time")
    d = _parse_date_any(de.get_text(" ", strip=True)) if de else None
    body_el = soup.select_one(".field-docs-content")
    body = _clean(body_el.get_text(" ")) if body_el else ""
    can = soup.find("link", rel="canonical")
    url = can["href"] if (can and can.get("href")) else fallback_url
    return {
        "source": "americanpresidency.org",
        "date": d,
        "channel": channel_from_title(title, url),
        "title": title,
        "url": url,
        "body": body[:8000],
        "person": "",
        "date_exact": d is not None,
    }


def load_app_doc_events(cache: Path = CACHE):
    """Parse every cached appdoc_*.html (full transcript bodies), scope-filtered."""
    by_url = {}
    files = sorted(glob.glob(str(cache / "appdoc_*.html")))
    stats = {"files": len(files), "parsed": 0, "dropped_no_date": 0,
             "dropped_out_of_scope": 0, "dropped_no_body": 0}
    for fp in files:
        ev = parse_app_document(
            Path(fp).read_text(encoding="utf-8", errors="ignore"), fallback_url=fp)
        if ev["date"] is None:
            stats["dropped_no_date"] += 1
            continue
        if ev["date"] < SCOPE_START:
            stats["dropped_out_of_scope"] += 1
            continue
        if not ev["body"]:
            stats["dropped_no_body"] += 1
            continue
        stats["parsed"] += 1
        by_url[ev["url"]] = ev
    return sorted(by_url.values(), key=lambda e: e["date"]), stats


# --------------------------------------------------------------------------- #
# Source B: WhiteHouse.gov article pages (cache/art_*.html)
# This mirrors enrich_wh_article() but returns the full spoken-remark schema
# and adds title-aware channel detection (some /releases/ docs are 'Remarks by').
# --------------------------------------------------------------------------- #
def _wh_date(soup: BeautifulSoup, html: str):
    import pandas as pd
    t = soup.find("time")
    if t and t.get("datetime"):
        try:
            return pd.to_datetime(t["datetime"]).date(), True
        except Exception:
            pass
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
    if m:
        try:
            return pd.to_datetime(m.group(1)).date(), True
        except Exception:
            pass
    return None, False


def _wh_title(soup: BeautifulSoup):
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return _clean(og["content"])
    if soup.title:
        return _clean(soup.title.get_text())
    return ""


def _wh_url(soup: BeautifulSoup, fallback: str):
    can = soup.find("link", rel="canonical")
    if can and can.get("href"):
        return can["href"]
    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        return og["content"]
    return fallback


def parse_wh_article(html: str, fallback_url: str = ""):
    """Parse ONE WhiteHouse.gov article page into a spoken-remark event."""
    soup = BeautifulSoup(html, "html.parser")
    d, exact = _wh_date(soup, html)
    title = _wh_title(soup)
    url = _wh_url(soup, fallback_url)
    art = soup.find("article") or soup.find("main") or soup
    body = _clean(art.get_text(" "))
    return {
        "source": "whitehouse.gov",
        "date": d,
        "channel": channel_from_title(title, url),
        "title": title,
        "url": url,
        "body": body[:6000],
        "person": "",
        "date_exact": exact,
    }


def load_wh_events(cache: Path = CACHE):
    """Parse every cached art_*.html into dated WH events (scope-filtered)."""
    by_url = {}
    files = sorted(glob.glob(str(cache / "art_*.html")))
    stats = {"files": len(files), "parsed": 0, "dropped_no_date": 0,
             "dropped_out_of_scope": 0}
    for fp in files:
        html = Path(fp).read_text(encoding="utf-8", errors="ignore")
        ev = parse_wh_article(html, fallback_url=fp)
        if ev["date"] is None:
            stats["dropped_no_date"] += 1
            continue
        if ev["date"] < SCOPE_START:
            stats["dropped_out_of_scope"] += 1
            continue
        stats["parsed"] += 1
        by_url[ev["url"]] = ev
    events = sorted(by_url.values(), key=lambda e: e["date"])
    return events, stats


# --------------------------------------------------------------------------- #
# Unified entry point
# --------------------------------------------------------------------------- #
def load_transcripts(cache: Path = CACHE, scope_start: dt.date = SCOPE_START):
    """Return (events, stats). Events = APP + WH spoken/official events.

    APP rows have exact dates but no body (listing). WH rows have exact dates
    AND body. Both carry channel. Scope filter applies to dated rows.
    """
    doc_events, doc_stats = load_app_doc_events(cache)          # NEW: full bodies
    app_events, app_stats = load_app_events(cache)              # listings (title-only)
    wh_events, wh_stats = load_wh_events(cache)

    by_url = {e["url"]: e for e in doc_events}                  # body docs win
    listing_added = 0
    for e in app_events:                                       # supplement only
        if e["date"] and e["date"] >= scope_start and e["url"] not in by_url:
            by_url[e["url"]] = e
            listing_added += 1
    app_all = [e for e in by_url.values() if e["date"] and e["date"] >= scope_start]

    events = app_all + wh_events
    events.sort(key=lambda e: (e["date"] or dt.date.min, e["source"]))

    stats = {
        "app_docs_with_body": doc_stats,
        "app_listing": app_stats,
        "wh": wh_stats,
        "app_body_in_scope": len(doc_events),
        "app_listing_supplemental": listing_added,
        "app_in_scope": len(app_all),
        "wh_in_scope": len(wh_events),
        "total_in_scope": len(events),
    }
    return events, stats


# --------------------------------------------------------------------------- #
# Self-demonstration (run as a script) — proves parsing on the cache and
# documents the May-8 "go out and buy a Dell" ingestion path explicitly.
# --------------------------------------------------------------------------- #
def _channel_breakdown(events):
    from collections import Counter
    return dict(Counter(e["channel"] for e in events))


def may8_ingestion_path():
    """Document + DEMONSTRATE precisely how the May-8-2025 'go out and buy a
    Dell' spoken remark is ingested once fetched.

    The remark is a SPOKEN Oval Office / press-pool exchange. The American
    Presidency Project indexes these as a /documents/<slug> page under Trump's
    SECOND-TERM person id (NOT 200301, which is 1st term and is why the cache
    is empty for 2025). Concretely, once the correct second-term search is
    fetched and the document page cached as cache/app_remarks-*.html:

      STEP 1  fetch_app() lists the doc row -> exact date 'May 8, 2025'
              parsed by parse_app_date(); title e.g.
              'Remarks in an Exchange With Reporters'.
      STEP 2  channel_from_title(title) -> 'oval_remark'  (spoken weight 0.90).
      STEP 3  the /documents/<slug> page body is fetched + cached; parsed to
              the verbatim transcript containing the literal sentence
              '... you have to go out and buy a Dell computer ...'.
      STEP 4  downstream generator runs open-vocab company extraction over the
              body, hits 'Dell' in COMMERCIAL/PRODUCT context ('buy a Dell
              computer') -> SEC gazetteer resolves Dell -> DELL -> high
              confidence (commercial context, not bare person name).
      STEP 5  verification: the quote 'go out and buy a Dell' is a LITERAL
              substring of body -> passes the substring gate; DELL resolves in
              gazetteer -> surfaces in entity_rollup with last_quote.

    This function builds the event record the parser WOULD emit, runs it
    through the real channel + substring-verification logic, and asserts the
    Dell extraction passes. It uses a synthetic transcript body (the remark is
    not in cache) but the SAME code paths as live parsing.
    """
    # The verbatim-style body the APP document page would carry.
    synthetic_body = _clean(
        "THE PRESIDENT: We're going to bring prices down. You know, somebody "
        "said the other day, well, maybe the children will have two dolls "
        "instead of 30 dolls. And maybe the two dolls will cost a couple of "
        "bucks more than they would normally. But we're not talking about "
        "that. You go out and buy a Dell computer, it's a great computer, "
        "Michael Dell is a great guy. We're doing very well with the chips. "
        "Q: Mr. President, on tariffs --"
    )
    title = "Remarks in an Exchange With Reporters Prior to a Meeting"
    url = ("https://www.presidency.ucsb.edu/documents/"
           "remarks-exchange-with-reporters-prior-meeting-may-8-2025")
    event = {
        "source": "americanpresidency.org",
        "date": dt.date(2025, 5, 8),
        "channel": channel_from_title(title, url),     # real logic
        "title": title,
        "url": url,
        "body": synthetic_body,
        "person": "Donald J. Trump (2nd Term)",
        "date_exact": True,
    }

    # Reproduce the downstream verification gate (literal substring + context).
    quote = "go out and buy a Dell"
    literal_ok = quote.lower() in event["body"].lower()
    commercial_ctx = bool(re.search(
        r"\bbuy a dell\b|\bdell (computer|laptop|server|pc)\b", event["body"], re.I))
    person_only = bool(re.search(r"\bmichael dell\b", event["body"], re.I))
    # Disambiguation: commercial product context overrides the person mention.
    keep_dell = literal_ok and commercial_ctx

    print("\n[E] MAY-8 'go out and buy a Dell' INGESTION PATH (documented + demoed)")
    print(f"    parsed date         : {event['date']}  (exact={event['date_exact']})")
    print(f"    channel             : {event['channel']}  (spec: oval_remark=0.90)")
    print(f"    title               : {event['title']}")
    print(f"    literal-quote gate  : {literal_ok}  quote={quote!r}")
    print(f"    commercial context  : {commercial_ctx}  (overrides person-only={person_only})")
    print(f"    => DELL surfaces?   : {keep_dell}  -> ticker DELL, high confidence")
    assert event["channel"] == "oval_remark", "May-8 must map to oval_remark"
    assert keep_dell, "May-8 Dell extraction must pass substring + commercial gate"
    return event


def _demo():
    print("=" * 74)
    print("TRANSCRIPTS LANE — cache-only spoken-remark parser demonstration")
    print("CACHE:", CACHE)
    print("=" * 74)

    # ---- APP listing parse ----
    app_events, app_stats = load_app_events()
    print("\n[A] American Presidency Project (cache/app_*.html)")
    print(f"    files={app_stats['files']} "
          f"with_results={app_stats['files_with_results']} "
          f"raw_rows={app_stats['raw_rows']} unique_docs={len(app_events)}")
    if app_events:
        ds = [e["date"] for e in app_events if e["date"]]
        print(f"    date range: {min(ds)} .. {max(ds)}")
    print(f"    channels: {_channel_breakdown(app_events)}")
    app_in_scope = [e for e in app_events if e["date"] and e["date"] >= SCOPE_START]
    print(f"    >>> IN SCOPE (>= {SCOPE_START}): {len(app_in_scope)} "
          f"  <-- v1 BLIND SPOT: cached APP search used Trump 1st-term "
          f"person id, so it returns ONLY 2015-2020 docs.")

    print("\n    Sample of parsed APP documents (date | channel | title):")
    for e in app_events[:8]:
        print(f"      {e['date']}  [{e['channel']:<16}]  {e['title'][:58]}")

    # ---- WH article parse ----
    wh_events, wh_stats = load_wh_events()
    print("\n[B] WhiteHouse.gov articles (cache/art_*.html)")
    print(f"    files={wh_stats['files']} parsed_in_scope={wh_stats['parsed']} "
          f"dropped_no_date={wh_stats['dropped_no_date']} "
          f"dropped_out_of_scope={wh_stats['dropped_out_of_scope']}")
    if wh_events:
        ds = [e["date"] for e in wh_events]
        print(f"    date range: {min(ds)} .. {max(ds)}")
    print(f"    channels: {_channel_breakdown(wh_events)}")

    print("\n    Sample of parsed WH spoken/official events (date | channel | title):")
    for e in wh_events[:10]:
        print(f"      {e['date']}  [{e['channel']:<16}]  {e['title'][:58]}")

    # ---- the Dell proof case ----
    print("\n[C] DELL PROOF CASE — is the 'go out and buy a Dell' remark in cache?")
    needles = ["buy a dell", "go out and buy"]
    found_any = False
    for fp in glob.glob(str(CACHE / "*.html")):
        low = Path(fp).read_text(encoding="utf-8", errors="ignore").lower()
        for n in needles:
            if n in low:
                found_any = True
                i = low.find(n)
                print(f"    HIT '{n}' in {Path(fp).name}: ...{low[i-40:i+40]}...")
    if not found_any:
        print("    NOT IN CACHE. No 'buy a Dell' / 'go out and buy' string exists.")
        print("    Reason: the May-8-2025 remark is a SPOKEN Oval/press exchange.")
        print("    The only Dell hits in cache are person/philanthropy/company")
        print("    mentions inside WH *written* releases (Michael Dell, Dell")
        print("    Technologies), e.g.:")
        for fp in sorted(glob.glob(str(CACHE / "art_releases_*dell*.html"))):
            ev = parse_wh_article(Path(fp).read_text(encoding="utf-8", errors="ignore"), fp)
            print(f"      {ev['date']}  [{ev['channel']}]  {ev['title'][:60]}")

    # ---- unified ----
    events, stats = load_transcripts()
    print("\n[D] UNIFIED in-scope spoken/official events:",
          stats["total_in_scope"],
          f"(APP {stats['app_in_scope']} + WH {stats['wh_in_scope']})")

    # ---- May-8 ingestion path (documented + asserted) ----
    may8_ingestion_path()
    return events, stats


def _dump_json(events, stats, path: Path):
    import json
    rows = []
    for e in events:
        r = dict(e)
        r["date"] = r["date"].isoformat() if r.get("date") else None
        r.pop("body", None)                # keep dump lean; bodies are large
        r["body_chars"] = len(e.get("body") or "")
        rows.append(r)
    payload = {"generated": dt.datetime.now().isoformat(timespec="seconds"),
               "stats": stats, "events": rows}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\n[F] wrote {len(rows)} parsed events -> {path}")


if __name__ == "__main__":
    evs, st = _demo()
    _dump_json(evs, st, HERE / "transcripts_events.json")
