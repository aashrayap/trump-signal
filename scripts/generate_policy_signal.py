#!/usr/bin/env python3
"""
Trump Policy-Signal generator (v3 rebuild).

Reframe (locked): a weighted ATTENTION feed built from Trump's *words* --
NOT a trade signal, NOT a predicted return. Every surfaced company carries a
verified quote (literal substring of the source doc) and a ticker that resolves
in the SEC gazetteer. The entity layer is strictly DESCRIPTIVE ("he named X
here, this often"); it never infers "policy helps X".

Three source families of Trump communications:
  (1) Truth Social posts        -> local data/posts_all.csv  (~9,287 rows)
  (2) Official WH releases       -> cache/art_*.html  (+ optional live fetch)
  (3) Spoken transcripts (APP)   -> cache/app_*.html via transcripts.load_transcripts()

Pipeline (deterministic-first; LLM is a cached refinement ONLY):
  load 3 sources
    -> for each doc: extractor.extract(doc_text, channel) -> verified
       {themes, companies[name,ticker,confidence,quote,is_person_only], discards}
    -> corpus-level frequency per ticker -> finalize confidence
    -> confidence threshold (LOG every discard with a reason; no silent cuts)
    -> weight each event  (impact_weight = channel x specificity x materiality)
    -> rollups: entity (confidence + last_quote), theme, timeline (week + month),
       decay-weighted standings, week-over-week momentum, "new this period"
    -> market_context (top ~12 surfaced + DELL)
    -> ONE xlsx (events, entity_rollup, theme_rollup, timeline, market_context,
                 source_weights, discards)
    -> ONE human brief in the v3 format.

The hot path (weight / rollup / render) is pure code. Any LLM extraction is
cached per doc-hash under cache/llm_extract/ by the extractor; we never call
the network in the hot path.

Usage:
  python3 generate_policy_signal.py --no-fetch        # cache + gazetteer only (default-safe, fast)
  python3 generate_policy_signal.py                    # allow live WH/APP/nasdaq fetches (cached)
  python3 generate_policy_signal.py --window-weeks 16
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]            # repo root
SCRIPTS = Path(__file__).resolve().parent             # .../scripts
DATA = ROOT / "data"
POSTS_CSV = DATA / "posts_all.csv"
PRICES_CSV = DATA / "market_prices.csv"
OUT = ROOT / "outputs"
CACHE = ROOT / "cache"
BRIEF_PATH = ROOT / "policy-signal-brief.md"
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)

# Import the three spike modules. They live next to this generator under
# scripts/; make scripts/ importable regardless of cwd.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import extractor as EX           # noqa: E402  (open-vocab company + theme extractor)
import transcripts as TR         # noqa: E402  (spoken/official transcript ingestion)
from resolver import get_resolver  # noqa: E402

HEADERS = {"User-Agent": "Mozilla/5.0 (ai-investment-system policy-signal research)"}
SCOPE_START = dt.date(2025, 1, 20)

# --------------------------------------------------------------------------- #
# Weighting config  (KEPT from v2; mirrored into the source_weights tab)
#   impact_weight = channel x specificity x materiality  (x deleted penalty)
# --------------------------------------------------------------------------- #
CHANNEL_W = {
    "exec_action":      1.00,   # signed EO / presidential action (binding)
    "oval_remark":      0.90,   # Oval / pool spray / press exchange (spoken)
    "official_release": 0.80,   # WH press release / fact sheet (written official)
    "speech":           0.70,   # scheduled speech / rally / interview (spoken)
    "social_post":      0.50,   # Truth Social original, policy-bearing
    "social_praise":    0.20,   # Truth Social original, praise / personal
    "social_repost":    0.15,   # Truth Social repost / quote
}
SPEC_W = {"entity": 1.00, "product_person": 0.80, "sector": 0.60, "macro": 0.30}
DELETED_PENALTY = 0.50

# Theme -> materiality weight (the extractor owns the regex; we own the weight).
# All buckets 1.0 except manufacturing 0.6 (locked spec).
THEME_MATERIALITY = dict(EX.THEME_MATERIALITY)   # single source of truth = extractor

# Confidence floor for surfacing a company after corpus-frequency finalization.
KEEP_CONFIDENCE = 0.35
# Per-doc extractor floor: keep a touch below the final gate so a strong single
# mention survives long enough to be finalized (we re-gate after frequency).
EXTRACT_FLOOR = 0.30

# Half-life (in days) for decay-weighted standings: recent attention counts more.
DECAY_HALF_LIFE_DAYS = 45.0

# --------------------------------------------------------------------------- #
# Generator-side proper-noun collision suppressor (DESIGN #4, the generator's
# confidence-model lane). The gazetteer/resolver intentionally LEAVES surname/
# place collisions for us: a kept company whose MATCHED SURFACE is a bare
# geography (country / US state / region / county) or a generic abstract noun
# is almost always Trump naming the *place/concept*, not the micro-cap whose
# distinctive gazetteer name happens to equal that word ("Taiwan" -> TWN fund,
# "New Jersey" -> NJR, "emerging" -> EMRH, "Artificial Intelligence" -> AITX).
# We DROP these and LOG them (no silent cut). Genuine multi-word corporates
# ("Taiwan Semiconductor Manufacturing", "General Motors") are unaffected: their
# matched surface is the full distinctive name, not the bare place word.
_COUNTRIES = {
    "china", "taiwan", "japan", "korea", "south korea", "north korea", "india",
    "indonesia", "vietnam", "thailand", "malaysia", "philippines", "singapore",
    "mexico", "canada", "brazil", "argentina", "venezuela", "colombia", "panama",
    "russia", "ukraine", "germany", "france", "italy", "spain", "england",
    "britain", "ireland", "greece", "turkey", "egypt", "israel", "iran", "iraq",
    "syria", "yemen", "saudi arabia", "qatar", "kuwait", "australia",
    "new zealand", "switzerland", "sweden", "norway", "denmark", "finland",
    "poland", "hungary", "austria", "belgium", "netherlands", "portugal",
    "nigeria", "kenya", "ethiopia", "morocco", "pakistan", "bangladesh",
    "afghanistan", "cuba", "haiti", "jamaica", "peru", "chile", "ecuador",
}
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}
_REGIONS = {
    "europe", "asia", "africa", "america", "americas", "middle east",
    "the middle east", "latin america", "the west", "the south", "the north",
    "scandinavia", "the gulf", "the caribbean", "eurasia", "the balkans",
    "greene county", "main street", "wall street",
}
# Generic abstract / common nouns that resolve to a micro-cap distinctive name
# but are virtually never that company in this corpus (demonstrated collisions).
_ABSTRACT_NOUNS = {
    "emerging", "artificial intelligence", "data", "leader", "complete",
    "island", "home", "city", "union", "united", "here", "landmark", "wealth",
    "capital", "international", "global", "national", "world", "freedom",
    "independence", "prosperity", "quality", "milestone", "equity", "balance",
    "research", "innovative", "visionary", "direct", "distribution",
    "community", "commerce", "service", "senior", "advantage", "core",
    "first", "next", "open", "power", "fair", "clean", "general", "american",
    "main", "grande",
}
GEO_ABSTRACT_BLOCK = _COUNTRIES | _US_STATES | _REGIONS | _ABSTRACT_NOUNS

# A few surnames-as-bare-name collisions that surfaced micro-caps (matched
# surface is exactly a person/place surname; the gazetteer carries them as a
# distinctive name so they aren't gated). DROP + LOG when the matched surface
# is exactly one of these bare words.
SURNAME_BLOCK = {
    "latham", "simpson", "crawford", "jackson", "madison", "franklin",
    "carter", "tyler", "taylor", "hudson", "ellington", "bragg", "mueller",
    "princeton", "dave", "megan",
}


def geo_abstract_reason(surface_name: str):
    """Return a discard reason if a matched surface is a bare place/abstract/
    surname collision, else None. Keyed on the MATCHED SURFACE (lowercased)."""
    s = re.sub(r"\s+", " ", (surface_name or "").strip().lower())
    if s in GEO_ABSTRACT_BLOCK:
        return ("geographic/abstract collision (matched surface is a "
                "place/common noun, not the company)")
    if s in SURNAME_BLOCK:
        return ("surname collision (matched surface is a bare person/place "
                "name, not the company)")
    return None


# --------------------------------------------------------------------------- #
# Specificity / materiality from an extractor result
# --------------------------------------------------------------------------- #
def specificity_for(companies, themes) -> float:
    """entity if a named company; product_person if a person-flagged company;
    sector if only themes; macro otherwise."""
    if companies:
        # a kept company that is ONLY a person mention is product_person, else entity
        if all(c.get("is_person_only") for c in companies):
            return SPEC_W["product_person"]
        return SPEC_W["entity"]
    if themes:
        return SPEC_W["sector"]
    return SPEC_W["macro"]


def materiality_for(themes) -> float:
    if themes:
        return max(THEME_MATERIALITY.get(t, 0.3) for t in themes)
    return 0.10


def impact_weight(channel, spec, mat, deleted=False) -> float:
    w = CHANNEL_W.get(channel, 0.3) * spec * mat
    if deleted:
        w *= DELETED_PENALTY
    return round(w, 4)


# --------------------------------------------------------------------------- #
# Source 1: Truth Social posts (local csv)
# --------------------------------------------------------------------------- #
def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not pd.isna(v):
        return bool(v)
    return str(v).strip().lower() in {"true", "1", "yes", "t"}


def social_channel(text: str, repost: bool, themes) -> str:
    """Classify a Truth Social post into a social channel.

    repost -> social_repost; policy-bearing (a theme fired) -> social_post;
    otherwise praise/personal -> social_praise.
    """
    if repost:
        return "social_repost"
    if themes and materiality_for(themes) >= 0.6:
        return "social_post"
    return "social_praise"


def load_social_docs() -> list:
    """Return raw social docs [{date, channel?, text, title, url, is_deleted, is_repost}]
    for the extractor. Channel is finalized after theme detection per doc."""
    df = pd.read_csv(POSTS_CSV)
    df["text"] = df["text"].fillna("").astype(str)
    df["title"] = df.get("title", "").fillna("").astype(str)
    df["date_et"] = pd.to_datetime(df["date_et"], errors="coerce").dt.date
    docs = []
    for _, p in df.iterrows():
        text = (str(p.get("title", "")) + " " + p["text"]).strip()
        if not text:
            continue
        docs.append({
            "source": "truth_social",
            "date": p["date_et"],
            "text": text,
            "title": str(p.get("title", ""))[:120],
            "url": p.get("archive_url", "") or p.get("original_url", "") or "",
            "is_deleted": _bool(p.get("is_deleted", False)),
            "is_repost": _bool(p.get("is_repost", False)),
        })
    return docs


# --------------------------------------------------------------------------- #
# Source 2 + 3: Official WH releases + spoken transcripts (via transcripts.py)
# --------------------------------------------------------------------------- #
def cached_get(url: str, key: str, no_fetch: bool):
    cf = CACHE / (re.sub(r"[^a-zA-Z0-9_.-]", "_", key)[:120] + ".html")
    if cf.exists():
        return cf.read_text(encoding="utf-8", errors="ignore")
    if no_fetch:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200 and r.text:
            cf.write_text(r.text, encoding="utf-8")
            time.sleep(0.4)
            return r.text
    except Exception as e:
        print(f"    fetch fail {url}: {type(e).__name__}", file=sys.stderr)
    return None


def load_official_and_transcript_docs(no_fetch: bool) -> list:
    """Official WH releases (cache/art_*.html) + spoken APP transcripts
    (cache/app_*.html), parsed + dated by the transcripts lane. Each carries a
    body + channel; APP listing rows have no body (title only) -- documented v1
    blind spot for 2nd-term spoken remarks.

    Optional live fetch (no_fetch=False) is a thin best-effort top-up of WH
    search results; the cache already carries 86 dated in-scope events.
    """
    events, stats = TR.load_transcripts(cache=CACHE, scope_start=SCOPE_START)
    docs = []
    for e in events:
        body = e.get("body") or ""
        # APP listing rows have title only; use the title as the text so the
        # channel/theme still register (low-body, low specificity).
        text = body if body else (e.get("title") or "")
        docs.append({
            "source": e["source"],
            "date": e["date"],
            "channel": e["channel"],          # already mapped by transcripts lane
            "text": text,
            "title": (e.get("title") or "")[:160],
            "url": e.get("url", ""),
            "is_deleted": False,
            "is_repost": False,
            "date_approx": not e.get("date_exact", True),
            "has_body": bool(body),
        })
    return docs, stats


# --------------------------------------------------------------------------- #
# Extraction pass: run the hybrid extractor over every doc.
# Returns (events_df, raw_company_rows, discard_counter).
#   events_df: one row per doc with weight + theme/entity strings (for rollups)
#   raw_company_rows: one row per (doc, kept company) with confidence + quote
#   discard_counter: Counter[(name, ticker, reason)] -> count  (audit, aggregated)
# --------------------------------------------------------------------------- #
def run_extraction(docs: list, *, use_llm: bool) -> tuple:
    event_rows = []
    company_rows = []
    discard_counter = Counter()
    R = get_resolver()  # warm the singleton once

    for d in docs:
        text = d["text"]
        # finalize social channel using theme detection; official/transcript
        # docs already carry their channel from the transcripts lane.
        if "channel" in d and d["channel"]:
            channel = d["channel"]
        else:
            themes_probe = EX.classify_themes(text)
            channel = social_channel(text, d.get("is_repost", False), themes_probe)
        deleted = bool(d.get("is_deleted", False))
        eff_channel = channel  # deleted penalty applied at weight time, not channel swap

        res = EX.extract(
            text,
            channel=eff_channel,
            keep_threshold=EXTRACT_FLOOR,
            use_llm=use_llm,
        )
        themes = res["themes"]

        # ---- generator-side geography/abstract/surname collision suppressor --
        # (the confidence-model lane the gazetteer leaves to us). Drop + LOG;
        # never a silent cut.
        companies = []
        for c in res["companies"]:
            reason = geo_abstract_reason(c["name"])
            if reason:
                discard_counter[(c["name"], c["ticker"], reason)] += 1
                continue
            companies.append(c)

        # aggregate extractor discards (never silent; aggregated for a usable audit)
        for dc in res["discards"]:
            discard_counter[(dc["name"], dc.get("ticker"), dc["reason"])] += 1

        spec = specificity_for(companies, themes)
        mat = materiality_for(themes)
        w = impact_weight(channel, spec, mat, deleted)

        tickers = sorted({c["ticker"] for c in companies})
        event_rows.append({
            "date": d["date"],
            "source": d["source"],
            "channel": channel,
            "entities": ";".join(tickers),
            "themes": ";".join(themes),
            "specificity": spec,
            "materiality": mat,
            "is_deleted": deleted,
            "impact_weight": w,
            "title": d.get("title", "")[:120],
            "text": text[:280],
            "url": d.get("url", ""),
            "date_approx": bool(d.get("date_approx", False)),
            "backend": res["backend"],
        })

        for c in companies:
            company_rows.append({
                "date": d["date"],
                "source": d["source"],
                "channel": channel,
                "ticker": c["ticker"],
                "name": c["name"],
                "doc_confidence": c["confidence"],   # extractor: sharpness x channel x gaz
                "quote": c["quote"][:160],
                "is_person_only": bool(c.get("is_person_only")),
                "impact_weight": w,
                "url": d.get("url", ""),
            })

    events = pd.DataFrame(event_rows)
    companies_df = pd.DataFrame(company_rows)
    return events, companies_df, discard_counter


# --------------------------------------------------------------------------- #
# Corpus frequency + confidence finalization (DESIGN #4).
#   final_confidence = doc_confidence_max  x  freq_factor
#     freq_factor in [0.7 .. 1.0]; a name said across many DISTINCT docs is more
#     trustworthy. A single strong mention (Dell @0.81) stays high (x0.7 ~ 0.57).
# --------------------------------------------------------------------------- #
def freq_factor(doc_count: int) -> float:
    # 1 doc -> 0.70 ; 2 -> 0.81 ; 4 -> 0.92 ; 8+ -> ~1.0   (log2 growth, capped)
    return round(min(1.0, 0.70 + 0.105 * math.log2(doc_count + 1)), 3)


def finalize_companies(companies_df: pd.DataFrame) -> pd.DataFrame:
    """Per kept company row, attach corpus frequency + finalized confidence."""
    if companies_df.empty:
        return companies_df.assign(doc_freq=[], freq_factor=[], final_confidence=[])
    docfreq = companies_df.groupby("ticker")["url"].nunique()
    # if urls are blank/duplicated, fall back to row count per ticker
    rowfreq = companies_df.groupby("ticker").size()
    eff_freq = docfreq.where(docfreq > 0, rowfreq).clip(lower=1)
    c = companies_df.copy()
    c["doc_freq"] = c["ticker"].map(eff_freq).astype(int)
    c["freq_factor"] = c["doc_freq"].map(freq_factor)
    c["final_confidence"] = (c["doc_confidence"] * c["freq_factor"]).round(3)
    return c


# --------------------------------------------------------------------------- #
# Rollups
# --------------------------------------------------------------------------- #
def explode_col(events, col):
    e = events.copy()
    e[col] = e[col].fillna("").astype(str)
    e = e.assign(**{col: e[col].str.split(";")}).explode(col)
    e[col] = e[col].str.strip()
    return e[e[col] != ""]


def _decay_weight(d, as_of, half_life=DECAY_HALF_LIFE_DAYS):
    if d is None or pd.isna(d):
        return 0.0
    age = (as_of - d).days
    if age < 0:
        age = 0
    return 0.5 ** (age / half_life)


def entity_rollup(companies_df: pd.DataFrame, events: pd.DataFrame,
                  as_of: dt.date, window_start: dt.date) -> pd.DataFrame:
    """Company rollup from the FINALIZED company rows. Carries confidence +
    last_quote + decay-weighted attention + week-over-week momentum + mentions
    & active weeks + 'new this period' flag."""
    if companies_df.empty:
        return pd.DataFrame()
    c = companies_df.copy()
    c["dt"] = pd.to_datetime(c["date"], errors="coerce")
    c["week"] = c["dt"].dt.to_period("W").astype(str)
    c["decay"] = c["dt"].dt.date.map(lambda d: _decay_weight(d, as_of))
    c["decay_attn"] = c["decay"] * c["impact_weight"]

    last_week_start = as_of - dt.timedelta(days=7)
    prev_week_start = as_of - dt.timedelta(days=14)

    rows = []
    for tk, g in c.groupby("ticker"):
        g = g.sort_values("dt")
        last = g.iloc[-1]
        wk_now = g[g["dt"].dt.date >= last_week_start]["impact_weight"].sum()
        wk_prev = g[(g["dt"].dt.date >= prev_week_start) &
                    (g["dt"].dt.date < last_week_start)]["impact_weight"].sum()
        first_seen = g["dt"].min()
        rows.append({
            "ticker": tk,
            "name": last["name"],
            "confidence": round(g["final_confidence"].max(), 3),
            "mentions": int(len(g)),
            "weeks_active": int(g["week"].nunique()),
            "doc_freq": int(g["doc_freq"].iloc[0]),
            "attn_weight": round(g["impact_weight"].sum(), 3),
            "decay_attn": round(g["decay_attn"].sum(), 4),
            "wow_now": round(wk_now, 3),
            "wow_prev": round(wk_prev, 3),
            "wow_delta": round(wk_now - wk_prev, 3),
            "top_channel": g["channel"].value_counts().idxmax(),
            "sources": ";".join(sorted(g["source"].unique())),
            "first_seen": first_seen.date() if pd.notna(first_seen) else None,
            "last_seen": last["dt"].date() if pd.notna(last["dt"]) else None,
            "is_person_only": bool(g["is_person_only"].all()),
            "new_this_week": bool(pd.notna(first_seen) and first_seen.date() >= last_week_start),
            "last_quote": str(last["quote"]),
        })
    out = pd.DataFrame(rows)
    # gate by finalized confidence; log nothing here (these already passed the
    # extractor floor -- the < KEEP_CONFIDENCE drops are captured separately).
    out = out[out["confidence"] >= KEEP_CONFIDENCE]
    return out.sort_values(["decay_attn", "attn_weight"], ascending=False).reset_index(drop=True)


def low_confidence_drops(companies_df: pd.DataFrame) -> pd.DataFrame:
    """Companies that cleared the extractor floor but fell below the FINAL
    confidence gate after frequency finalization -> logged (no silent cut)."""
    if companies_df.empty:
        return pd.DataFrame()
    agg = companies_df.groupby("ticker").agg(
        name=("name", "last"),
        final_confidence=("final_confidence", "max"),
        mentions=("ticker", "size"),
    ).reset_index()
    return agg[agg["final_confidence"] < KEEP_CONFIDENCE].sort_values(
        "final_confidence", ascending=False)


def theme_rollup(events: pd.DataFrame, as_of: dt.date) -> pd.DataFrame:
    e = explode_col(events, "themes")
    if e.empty:
        return pd.DataFrame()
    e["dt"] = pd.to_datetime(e["date"], errors="coerce")
    e["decay"] = e["dt"].dt.date.map(lambda d: _decay_weight(d, as_of))
    e["decay_attn"] = e["decay"] * e["impact_weight"]
    last_week_start = as_of - dt.timedelta(days=7)
    prev_week_start = as_of - dt.timedelta(days=14)
    rows = []
    for th, g in e.groupby("themes"):
        wk_now = g[g["dt"].dt.date >= last_week_start]["impact_weight"].sum()
        wk_prev = g[(g["dt"].dt.date >= prev_week_start) &
                    (g["dt"].dt.date < last_week_start)]["impact_weight"].sum()
        rows.append({
            "theme": th,
            "mentions": int(len(g)),
            "attn_weight": round(g["impact_weight"].sum(), 2),
            "decay_attn": round(g["decay_attn"].sum(), 3),
            "wow_now": round(wk_now, 2),
            "wow_prev": round(wk_prev, 2),
            "wow_delta": round(wk_now - wk_prev, 2),
            "materiality": THEME_MATERIALITY.get(th, 0.3),
        })
    return pd.DataFrame(rows).sort_values("decay_attn", ascending=False).reset_index(drop=True)


def timeline(events: pd.DataFrame) -> pd.DataFrame:
    out = []
    for grain, code in [("week", "W"), ("month", "M")]:
        e = explode_col(events, "themes")
        if e.empty:
            continue
        e["period"] = pd.to_datetime(e["date"], errors="coerce").dt.to_period(code).astype(str)
        e = e[e["period"] != "NaT"]
        g = e.groupby(["period", "themes"]).agg(
            n=("impact_weight", "size"),
            weighted=("impact_weight", "sum")).reset_index()
        g["weighted"] = g["weighted"].round(3)
        g.insert(0, "grain", grain)
        g = g.rename(columns={"themes": "theme"})
        out.append(g)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# --------------------------------------------------------------------------- #
# Market context (reuse v2 nasdaq fetch + builder; surface top tickers + DELL)
# --------------------------------------------------------------------------- #
def fetch_nasdaq_daily(symbol: str, no_fetch: bool):
    key = f"nasdaq_{symbol}"
    cf = CACHE / f"{key}.json"
    raw = None
    if cf.exists():
        raw = cf.read_text(encoding="utf-8", errors="ignore")
    elif not no_fetch:
        url = (f"https://api.nasdaq.com/api/quote/{symbol}/historical"
               f"?assetclass=stocks&fromdate=2025-01-01&limit=9999&todate=2026-12-31")
        try:
            r = requests.get(url, headers={**HEADERS, "Accept": "application/json",
                             "Origin": "https://www.nasdaq.com",
                             "Referer": f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}/historical"},
                             timeout=30)
            if r.status_code == 200:
                raw = r.text
                cf.write_text(raw, encoding="utf-8")
                time.sleep(0.4)
        except Exception as e:
            print(f"    nasdaq fail {symbol}: {type(e).__name__}", file=sys.stderr)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        rows = data["data"]["tradesTable"]["rows"]
        recs = []
        for row in rows:
            close = float(re.sub(r"[^0-9.]", "", row["close"]))
            d = dt.datetime.strptime(row["date"], "%m/%d/%Y").date()
            recs.append({"symbol": symbol, "date": d, "close": close})
        return pd.DataFrame(recs).sort_values("date")
    except Exception as e:
        print(f"    nasdaq parse fail {symbol}: {type(e).__name__}", file=sys.stderr)
        return None


def load_prices(extra_tickers, no_fetch: bool) -> pd.DataFrame:
    frames = []
    if PRICES_CSV.exists():
        mp = pd.read_csv(PRICES_CSV)
        cols = {c.lower(): c for c in mp.columns}
        dcol = cols.get("date", "date")
        scol = cols.get("symbol", "symbol")
        ccol = cols.get("close", cols.get("adj_close", "close"))
        if dcol in mp and scol in mp and ccol in mp:
            mp = mp[[dcol, scol, ccol]].rename(
                columns={dcol: "date", scol: "symbol", ccol: "close"})
            mp["date"] = pd.to_datetime(mp["date"], errors="coerce").dt.date
            frames.append(mp)
    have = set(frames[0]["symbol"].unique()) if frames else set()
    for tk in extra_tickers:
        if tk and tk.isalpha() and tk not in have:
            f = fetch_nasdaq_daily(tk, no_fetch)
            if f is not None and not f.empty:
                frames.append(f)
                have.add(tk)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "symbol", "close"])


def pct(a, b):
    return None if (a is None or b in (None, 0) or pd.isna(a) or pd.isna(b)) \
        else round((a / b - 1) * 100, 1)


def build_market_context(surfaced, prices, window_start) -> pd.DataFrame:
    rows = []
    for tk in surfaced:
        sub = prices[prices["symbol"] == tk].sort_values("date") if not prices.empty \
            else prices
        if prices.empty or sub.empty:
            rows.append({"ticker": tk, "have_prices": False})
            continue
        sub = sub.dropna(subset=["close"])
        if sub.empty:
            rows.append({"ticker": tk, "have_prices": False})
            continue
        latest = sub.iloc[-1]
        win = sub[sub["date"] >= window_start]
        start_close = win.iloc[0]["close"] if not win.empty else sub.iloc[0]["close"]
        wk = sub[sub["date"] >= (latest["date"] - dt.timedelta(days=7))]
        hi = sub["close"].max()
        rows.append({
            "ticker": tk, "have_prices": True,
            "latest_date": latest["date"], "latest_close": round(latest["close"], 2),
            "ret_window_pct": pct(latest["close"], start_close),
            "ret_1w_pct": pct(latest["close"], wk.iloc[0]["close"] if not wk.empty else None),
            "pct_below_52w_high": pct(latest["close"], hi),
            "at_or_near_high": bool(latest["close"] >= hi * 0.99),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Workbook
# --------------------------------------------------------------------------- #
def source_weights_table() -> pd.DataFrame:
    rows = []
    for k, v in CHANNEL_W.items():
        rows.append({"dimension": "channel", "key": k, "weight": v})
    for k, v in SPEC_W.items():
        rows.append({"dimension": "specificity", "key": k, "weight": v})
    for t, m in THEME_MATERIALITY.items():
        rows.append({"dimension": "materiality", "key": t, "weight": m})
    rows.append({"dimension": "penalty", "key": "deleted_multiplier", "weight": DELETED_PENALTY})
    rows.append({"dimension": "gate", "key": "keep_confidence", "weight": KEEP_CONFIDENCE})
    rows.append({"dimension": "decay", "key": "half_life_days", "weight": DECAY_HALF_LIFE_DAYS})
    return pd.DataFrame(rows)


def discards_table(discard_counter: Counter, low_conf: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (name, ticker, reason), n in discard_counter.most_common():
        rows.append({"name": name, "ticker": ticker, "reason": reason, "count": n})
    df = pd.DataFrame(rows, columns=["name", "ticker", "reason", "count"])
    # append the post-frequency confidence drops (a distinct audit class)
    if low_conf is not None and not low_conf.empty:
        extra = pd.DataFrame([{
            "name": r["name"], "ticker": r["ticker"],
            "reason": f"final confidence {r['final_confidence']} < {KEEP_CONFIDENCE} (post-frequency)",
            "count": int(r["mentions"]),
        } for _, r in low_conf.iterrows()])
        df = pd.concat([df, extra], ignore_index=True)
    return df.sort_values("count", ascending=False).reset_index(drop=True)


def write_workbook(events, ent, thm, tl, mkt, discards) -> Path:
    path = OUT / "trump-policy-signal.xlsx"
    ev_cols = ["date", "source", "channel", "entities", "themes", "specificity",
               "materiality", "is_deleted", "impact_weight", "date_approx",
               "backend", "title", "text", "url"]
    ev_out = events[[c for c in ev_cols if c in events.columns]] \
        .sort_values("impact_weight", ascending=False)
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        ev_out.to_excel(xl, sheet_name="events", index=False)
        (ent if not ent.empty else pd.DataFrame({"_": ["(no companies surfaced)"]})) \
            .to_excel(xl, sheet_name="entity_rollup", index=False)
        (thm if not thm.empty else pd.DataFrame({"_": ["(no themes)"]})) \
            .to_excel(xl, sheet_name="theme_rollup", index=False)
        (tl if not tl.empty else pd.DataFrame({"_": ["(no timeline)"]})) \
            .to_excel(xl, sheet_name="timeline", index=False)
        (mkt if not mkt.empty else pd.DataFrame({"_": ["(no market context)"]})) \
            .to_excel(xl, sheet_name="market_context", index=False)
        source_weights_table().to_excel(xl, sheet_name="source_weights", index=False)
        (discards if not discards.empty else pd.DataFrame({"_": ["(no discards)"]})) \
            .to_excel(xl, sheet_name="discards", index=False)
    return path


# --------------------------------------------------------------------------- #
# Brief render (v3 format)
# --------------------------------------------------------------------------- #
def bar(x, lo, hi, width=10):
    if hi <= lo or x is None or pd.isna(x):
        return ""
    n = max(1, round((x - lo) / (hi - lo) * width))
    return "█" * n


def dots(conf, n=5):
    filled = max(0, min(n, round(conf * n)))
    return "●" * filled + "○" * (n - filled)


def momentum_arrow(delta, eps=0.05):
    if delta > eps:
        return "↑"      # up
    if delta < -eps:
        return "↓"      # down
    return "→"          # flat


def write_brief(events, ent, thm, tl, mkt, discards, counts, as_of,
                window_weeks, xlsx_path) -> Path:
    L = []
    A = L.append
    A(f"# Trump Policy-Signal — wk of {as_of}  ({window_weeks}-wk window)")
    A("")
    A("> Weighted ATTENTION from Trump words (posts + official + transcripts). "
      "NOT a trade signal, NOT predicted impact. Every name = a verified quote.")
    A("")

    # ---- New this week: top dated events in last 7d by impact_weight ----
    last_week_start = as_of - dt.timedelta(days=7)
    win = events.copy()
    win["d"] = pd.to_datetime(win["date"], errors="coerce").dt.date
    recent = win[win["d"] >= last_week_start].sort_values("impact_weight", ascending=False)
    A("## New this week")
    if recent.empty:
        A("- _(no dated events in the last 7 days)_")
    for _, r in recent.head(8).iterrows():
        tag = r["entities"] or r["themes"] or "—"
        marker = "▲" * (1 + int(round(r["impact_weight"] * 2)))
        approx = "~" if r.get("date_approx", False) else ""
        txt = re.sub(r"\s+", " ", str(r["text"]))[:90]
        A(f"- **{approx}{r['d']}** {marker} `{tag}` · {r['channel']} "
          f"(wt {r['impact_weight']:.2f}) — {txt}")
    A("")

    # ---- What he is pushing: themes (decay-weighted bars + momentum) ----
    A("## What he is pushing — themes")
    if thm.empty:
        A("- _(no themes fired)_")
    else:
        hi = thm["decay_attn"].max()
        lo = thm["decay_attn"].min()
        for _, r in thm.head(9).iterrows():
            arrow = momentum_arrow(r["wow_delta"])
            A(f"- `{r['theme']:<17}` {bar(r['decay_attn'], lo, hi)} {arrow}  "
              f"decay-wt {r['decay_attn']:.1f} · {int(r['mentions'])} mentions "
              f"(WoW {r['wow_now']:.1f} vs {r['wow_prev']:.1f})")
    A("")

    # ---- Companies he named (open-vocab, confidence-filtered) ----
    A("## Companies he named")
    A("_open-vocab · confidence-filtered · freq-weighted. ticker · "
      "confidence · attn-wt · mentions/weeks · last quote · price context_")
    A("")
    if ent.empty:
        A("- _(no companies cleared the confidence gate this run)_")
    else:
        mkt_map = mkt.set_index("ticker").to_dict("index") if not mkt.empty else {}
        for _, r in ent.head(15).iterrows():
            arrow = momentum_arrow(r["wow_delta"])
            person = " ⚠person" if r["is_person_only"] else ""
            new = " \U0001F195" if r.get("new_this_week") else ""
            m = mkt_map.get(r["ticker"], {})
            if m.get("have_prices"):
                price = (f" · {m.get('ret_window_pct')}% in window, "
                         f"${m.get('latest_close')}"
                         + (" \U0001F3AF ATH" if m.get("at_or_near_high") else ""))
            else:
                price = " · _no px_"
            q = re.sub(r"\s+", " ", str(r["last_quote"]))[:60]
            A(f"- **{r['ticker']:<6}** {dots(r['confidence'])} {arrow}{new}{person} · "
              f"attn {r['attn_weight']:.1f} · {int(r['mentions'])}m/{int(r['weeks_active'])}w "
              f"· “{q}”{price}")
    A("")

    # ---- Discarded this run: audit ----
    A("## Discarded this run — audit")
    if discards.empty:
        A("- _(nothing discarded)_")
    else:
        total_disc = int(discards["count"].sum())
        A(f"_{total_disc} discarded mentions across {len(discards)} (name, reason) "
          f"pairs — no silent cuts. Top:_")
        for _, r in discards.head(12).iterrows():
            tk = f" ({r['ticker']})" if pd.notna(r["ticker"]) and r["ticker"] else ""
            A(f"- `{r['name']}`{tk} ×{int(r['count'])} → {r['reason']}")
    A("")

    # ---- footer ----
    A("---")
    A(f"_Events: **{counts['total']}** "
      f"({counts['social']} social · {counts['official']} official · "
      f"{counts['transcripts']} transcripts). "
      f"Machine layer: `{xlsx_path.name}` "
      f"(tabs: events, entity_rollup, theme_rollup, timeline, market_context, "
      f"source_weights, discards). "
      f"Generated by `generate_policy_signal.py`._")

    path = BRIEF_PATH
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true",
                    help="use cache + gazetteer only (offline, guaranteed to run)")
    ap.add_argument("--window-weeks", type=int, default=16)
    ap.add_argument("--no-llm", action="store_true",
                    help="force deterministic extraction (skip cached Haiku refine)")
    args = ap.parse_args()
    use_llm = not args.no_llm

    # ---- 1) load 3 sources ----
    print("loading Truth Social posts (local csv)...")
    social_docs = load_social_docs()
    print(f"  social docs: {len(social_docs)}")

    print("loading official WH releases + spoken transcripts (cache)...")
    off_docs, tr_stats = load_official_and_transcript_docs(args.no_fetch)
    n_off = sum(1 for d in off_docs if d["source"] == "whitehouse.gov")
    n_tr = sum(1 for d in off_docs if d["source"] == "americanpresidency.org")
    print(f"  official/transcript docs: {len(off_docs)} "
          f"(WH {n_off}, APP {n_tr})  [APP in-scope from transcripts lane: "
          f"{tr_stats.get('app_in_scope', 0)}]")

    all_docs = social_docs + off_docs

    # ---- 2) extraction over every doc (hot path: deterministic; LLM cached) ----
    print(f"extracting companies + themes over {len(all_docs)} docs "
          f"(backend: {'haiku-if-available' if use_llm else 'deterministic'})...")
    events, companies_df, discard_counter = run_extraction(all_docs, use_llm=use_llm)
    print(f"  kept company mentions: {len(companies_df)} "
          f"({companies_df['ticker'].nunique() if not companies_df.empty else 0} distinct tickers); "
          f"discard classes: {len(discard_counter)}")

    # normalise event dates + as_of / window
    events["date"] = pd.to_datetime(events["date"], errors="coerce").dt.date
    valid = [d for d in events["date"] if pd.notna(d)]
    as_of = max(valid) if valid else dt.date.today()
    window_start = as_of - dt.timedelta(weeks=args.window_weeks)

    # ---- 3) corpus frequency + finalize confidence ----
    companies_df = finalize_companies(companies_df)
    low_conf = low_confidence_drops(companies_df)

    # ---- 4) rollups ----
    ent = entity_rollup(companies_df, events, as_of, window_start)
    thm = theme_rollup(events, as_of)
    tl = timeline(events)

    # ---- 5) market context: top ~12 surfaced + DELL ----
    surfaced = list(ent["ticker"].head(12)) if not ent.empty else []
    if "DELL" not in surfaced:
        surfaced.append("DELL")
    prices = load_prices(surfaced, args.no_fetch)
    mkt = build_market_context([t for t in surfaced if t], prices, window_start)

    # ---- 6) discards audit (aggregated + post-frequency drops) ----
    discards = discards_table(discard_counter, low_conf)

    # ---- 7) outputs ----
    counts = {
        "total": int(len(events)),
        "social": int((events["source"] == "truth_social").sum()),
        "official": int((events["source"] == "whitehouse.gov").sum()),
        "transcripts": int((events["source"] == "americanpresidency.org").sum()),
    }
    xlsx = write_workbook(events, ent, thm, tl, mkt, discards)
    brief = write_brief(events, ent, thm, tl, mkt, discards, counts,
                        as_of, args.window_weeks, xlsx)

    print(f"\nWROTE:\n  {xlsx}\n  {brief}")
    print(f"as_of={as_of}  window_start={window_start}  "
          f"events={counts}")
    if not thm.empty:
        print("top themes:", list(thm["theme"].head(6)))
    if not ent.empty:
        print("top companies:")
        print(ent[["ticker", "confidence", "attn_weight", "mentions",
                   "weeks_active"]].head(12).to_string(index=False))


if __name__ == "__main__":
    main()
