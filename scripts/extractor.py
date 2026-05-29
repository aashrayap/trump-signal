#!/usr/bin/env python3
"""
Hybrid open-vocab company + theme EXTRACTOR (EXTRACTION lane).

Contract
--------
    extract(doc_text, channel) -> {
        "themes":    [str, ...],                         # regex theme buckets (DESIGN #6)
        "companies": [
            {"name", "ticker", "confidence", "quote", "is_person_only"},
            ...                                           # only KEPT companies
        ],
        "discards":  [                                   # never a silent cut (DESIGN #4)
            {"name", "ticker"|None, "reason"},
            ...
        ],
        "backend":   "deterministic" | "haiku-4-5+deterministic",
    }

Design
------
Deterministic core is the source of truth (DESIGN #3):
  * Company matching is delegated to the GAZETTEER lane's `Resolver`
    (gazetteer.json = SEC company_tickers.json name->ticker). The resolver
    guarantees, for every Match it returns, that (a) `name` is a LITERAL
    substring of the doc (span-backed) and (b) `ticker` resolves in the
    gazetteer -- this mechanically satisfies the hard verification gate
    (DESIGN #5). We re-assert both in code anyway and DROP otherwise.
  * On top of the resolver this module adds the lane-specific judgment the
    resolver explicitly leaves to its consumer:
      - is_person_only  : "Michael Dell" (bare person) -> DROP, UNLESS a
                          company-action cue co-occurs (DESIGN #4).
      - soft-brand gate : brand-override names that are ALSO common English
                          nouns ("intel" = intelligence, "apple" = fruit)
                          require a SHARP company cue, not just any context,
                          so "FBI gives Congress intel" does not surface INTC.
      - unit/number gate: BILLION / MILLION / TRILLION etc. are real tickers
                          but almost always the English word here -> gate.
      - confidence      : blend of context_sharpness x frequency x channel x
                          gazetteer_agreement (DESIGN #4). Below threshold ->
                          logged discard, not silent.

Optional Haiku refine (DESIGN #3) is a CACHED, OPTIONAL refinement only. It
runs IF os.environ has ANTHROPIC_API_KEY AND the installed `anthropic` SDK
exposes the Messages API (claude-haiku-4-5). Otherwise it is skipped silently
and the deterministic result stands. Either way the code runs. Cache key is
sha1(model + doc_text) under cache/llm_extract/.

Pure-stdlib except the optional anthropic import. The hot path is pure code.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Optional

# ---- locate sibling resolver + cache --------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from resolver import get_resolver, Match  # noqa: E402

_LLM_CACHE_DIR = os.path.normpath(
    os.path.join(_HERE, "..", "cache", "llm_extract")
)

# ---- attack-target / media blocklist (hard-drop even if matched) ----------
# Real, liquid tickers whose Trump-mentions are political-attack / media noise,
# not investment signals (e.g. "Failing New York Times"). Loaded from
# attack_blocklist.json next to this module; dropped with an audit reason so the
# cut is visible, never silent.
_ATTACK_PATH = os.path.join(_HERE, "attack_blocklist.json")
try:
    ATTACK_BLOCKLIST = {k.upper(): v for k, v in json.load(open(_ATTACK_PATH)).items()}
except Exception:
    ATTACK_BLOCKLIST = {}

# ---------------------------------------------------------------------------
# THEMES -- regex buckets with materiality weights (DESIGN #6).
# Materiality is carried for the generator's weighting; extract() returns the
# theme keys, generator multiplies by these.
# ---------------------------------------------------------------------------
THEME_MATERIALITY = {
    "trade_tariffs": 1.0,
    "rates_macro": 1.0,
    "semis_ai_tech": 1.0,
    "energy_oil": 1.0,
    "defense_aero": 1.0,
    "iran_geopolitics": 1.0,
    "pharma_health": 1.0,
    "crypto": 1.0,
    "manufacturing": 0.6,
}

THEME_PATTERNS = {
    "trade_tariffs": re.compile(
        r"\b(tariffs?|trade\s+deficit|trade\s+war|import\s+dut(?:y|ies)|"
        r"customs|de\s*minimis|trade\s+deal|reciprocal|dumping|"
        r"section\s*232|section\s*301|usmca|wto)\b",
        re.I,
    ),
    "rates_macro": re.compile(
        r"\b(interest\s+rates?|federal\s+reserve|the\s+fed\b|jay\s+powell|"
        r"jerome\s+powell|\bpowell\b|rate\s+cut|basis\s+points?|inflation|"
        r"\bcpi\b|monetary\s+polic|too\s+late)\b",
        re.I,
    ),
    "semis_ai_tech": re.compile(
        r"\b(semiconductors?|\bchips?\b|\bfabs?\b|foundr(?:y|ies)|"
        r"artificial\s+intelligence|\bai\b|data\s*center?s?|gpus?|"
        r"\bnvidia\b|\btsmc\b|microchips?|wafer|\bcompute\b)\b",
        re.I,
    ),
    "energy_oil": re.compile(
        r"\b(oil|\bgas\b|\bdrill(?:ing)?\b|energy\s+independence|crude|opec|"
        r"pipeline|\blng\b|fossil\s+fuel|barrels?|petroleum|refiner(?:y|ies)|"
        r"natural\s+gas|energy\s+dominance)\b",
        re.I,
    ),
    "defense_aero": re.compile(
        r"\b(defense|military|missiles?|fighter\s+jets?|\bf-?35\b|golden\s+dome|"
        r"shipbuild|warships?|munitions?|aircraft|navy|army|air\s+force|"
        r"weapons?\s+system|nato|rearm)\b",
        re.I,
    ),
    "iran_geopolitics": re.compile(
        r"\b(iran|iranian|nuclear\s+(?:deal|weapon|program)|ayatollah|tehran|"
        r"\bhamas\b|hezbollah|\bisrael\b|gaza|houthi|middle\s+east|"
        r"strait\s+of\s+hormuz)\b",
        re.I,
    ),
    "pharma_health": re.compile(
        r"\b(drug\s+price?s?|prescription|pharmaceuticals?|\bpharma\b|"
        r"most\s+favored\s+nation|\bfda\b|medicare|medicaid|insulin|"
        r"big\s+pharma|biotech)\b",
        re.I,
    ),
    "crypto": re.compile(
        r"\b(bitcoin|crypto(?:currenc(?:y|ies))?|digital\s+assets?|blockchain|"
        r"\bethereum\b|stablecoins?|strategic\s+(?:bitcoin\s+)?reserve|"
        r"\bdefi\b|\bweb3\b|\$trump\b)\b",
        re.I,
    ),
    "manufacturing": re.compile(
        r"\b(manufacturing|factor(?:y|ies)|\bplants?\b|reshor|onshor|"
        r"made\s+in\s+america|american[-\s]made|assembly\s+line|"
        r"industrial\s+base|jobs?\s+back)\b",
        re.I,
    ),
}


def classify_themes(text: str) -> list:
    """Return theme keys whose regex fires, in DESIGN declaration order."""
    if not text:
        return []
    return [k for k in THEME_MATERIALITY if THEME_PATTERNS[k].search(text)]


# ---------------------------------------------------------------------------
# CONTEXT CUES for company judgment.
#   - SHARP_COMPANY_CUE : strong "this is the corporation" signal. Used to (a)
#     unlock soft-brand common-noun names (intel/apple) and (b) override
#     is_person_only when a person name co-occurs with a company action.
#   - COMMERCIAL_PRODUCT_CUE : "buy/use a <brand>" product/commercial sense.
#     This is what keeps "go out and buy a Dell" as DELL.
# ---------------------------------------------------------------------------
SHARP_COMPANY_CUE = re.compile(
    r"\b("
    r"stocks?|shares?|ticker|nasdaq|nyse|earnings|ipo|market[-\s]?cap|"
    r"share\s+price|valuation|dividends?|"
    r"\bceo\b|\bcfo\b|chairman|founder|headquarter(?:s|ed)?|"
    r"chips?|semiconductors?|microchips?|gpus?|wafer|foundr(?:y|ies)|"
    r"compan(?:y|ies)|corp(?:oration)?|\binc\b|holdings?|technolog(?:y|ies)|"
    r"factor(?:y|ies)|\bplants?\b|\bfabs?\b|manufactur\w*|"
    r"announced?|announc\w*|invest(?:ing|ed|ment|ments)?|"
    r"deals?|mergers?|acquir\w*|acquisitions?|\bbillion\b|\bmillion\b|"
    r"products?|laptops?|computers?|servers?|devices?|phones?"
    r")\b",
    re.I,
)

COMMERCIAL_PRODUCT_CUE = re.compile(
    r"\b("
    r"buy(?:ing|s)?|bought|purchas\w*|"
    r"laptops?|computers?|servers?|devices?|phones?|products?|"
    r"go\s+(?:out\s+and\s+)?(?:buy|get)|order(?:ed|ing)?\s+a"
    r")\b",
    re.I,
)

# STRICT, company-STRUCTURAL cue. Used ONLY to unlock an *ambiguous gated
# single word* (Apple, Box, Arm, …) and only when it sits TIGHTLY adjacent
# (<= STRICT_WINDOW chars) to the mention. A generic finance word merely
# present in a paragraph is NOT enough -- in a finance-dense WH release every
# token is near "investment"/"billion", which is why the loose cue produced
# microcap false positives (First/News/Global/here/track/work...). This set is
# deliberately structural: it names the company AS a corporation/product.
STRICT_BRAND_CUE = re.compile(
    r"\b("
    r"stocks?|ticker|nasdaq|nyse|earnings|ipo|market[-\s]?cap|"
    r"share\s+price|shareholders?|dividends?|"        # NOT bare "share(s)": collides with "Share Icon" nav
    r"\bceo\b|\bcfo\b|founder|founded|headquarter(?:s|ed)?|"
    r"chips?|semiconductors?|microchips?|gpus?|wafer|foundr(?:y|ies)|"
    r"smartphones?|iphone|"                           # NOT bare "server/computer": too generic for a 2-3char acronym
    r"announce[ds]|announcing|unveil\w*|"             # a COMPANY announces investments/products
    r"\bbuy\b|bought|purchas\w*|"
    r"board\s+of\s+directors"
    r")\b",
    re.I,
)
STRICT_WINDOW = 28

# Corporate descriptor words. If a GATED single word is immediately followed by
# one of these (e.g. "United Technologies", "American Industries"), the speaker
# named a LONGER company than the bare-word ticker the resolver captured -> the
# bare ticker is almost certainly the wrong company; drop it.
CORP_DESCRIPTOR_AFTER = re.compile(
    r"^\s+(Technolog(?:y|ies)|Group|Holdings?|Industries|Systems|Motors|"
    r"Corporation|Corp|Incorporated|Communications|Networks|Partners|"
    r"Solutions|Pharmaceuticals?|Airlines|Express|Financial|Bancorp|Energy)\b"
)

# Pure acronyms / theme tokens that resolve to *some* ticker but in this corpus
# are never the company (they are concepts/units). Hard local gate (logged).
ACRONYM_THEME_GATE = {
    "ai", "it", "us", "pc", "tv", "ev", "ceo", "cfo", "gdp", "cpi", "fbi",
    "doj", "fda", "epa", "irs", "nato", "un", "eu", "uk", "usa",
}

# Person-name pattern immediately preceding a surname-style company word.
#   "Michael Dell", "Michael S. Dell", "Michael and Susan Dell", "the Dells",
#   "Dell's gift" (possessive). We test the text immediately around the span.
_GIVEN = r"[A-Z][a-z]+"
PERSON_BEFORE = re.compile(
    r"(?:\b" + _GIVEN + r"\.?\s+){1,2}"
    r"(?:[A-Z]\.\s+)?"
    r"(?:and\s+" + _GIVEN + r"\s+)?$"
)
# given-name + "and" + (surname) ... e.g. "Michael and Susan <Surname>"
PERSON_AND = re.compile(r"\b" + _GIVEN + r"\s+and\s+" + _GIVEN + r"\s*$")

# Channel weights (DESIGN #6) -- carried so confidence can blend channel.
CHANNEL_WEIGHT = {
    "exec_action": 1.0,
    "oval_remark": 0.9,
    "official_release": 0.8,
    "speech": 0.7,
    "social_post": 0.5,
    "social_praise": 0.2,
    "social_repost": 0.15,
}

# Brand-override names (resolve bare in the resolver) that are ALSO ordinary
# English words -> require a SHARP company cue before we trust the company
# sense. Keeps "intel" (intelligence) from surfacing INTC. Dell/Micron/etc.
# are intentionally NOT here: they are dominated by the company sense and a
# mild product/commercial cue is enough for them.
SOFT_BRAND_COMMON_NOUN = {
    "intel",   # intelligence
    "apple",   # fruit (also blocklisted, but brand_override-safe here)
    "ford",    # ford a river
    "gap",     # a gap
    "block",   # to block
    "match",   # a match
    "target",  # a target
    "shell",   # a shell
}

# Surface tokens that resolve to a ticker but are number/unit/words that are
# essentially never the company in this corpus -> hard local gate (logged).
UNIT_NUMBER_GATE = {
    "billion", "million", "trillion", "thousand", "hundred",
    "one", "two", "three", "four", "five", "ten",
    "real", "great", "best", "good", "big", "huge", "very",
    "love", "win", "winning", "hope", "free", "safe", "true", "fun",
    "now", "next", "open", "live", "play",
}


def _doc_sha1(model: str, doc_text: str) -> str:
    h = hashlib.sha1()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(doc_text.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Optional, cached Haiku refinement. Returns a dict {name->ticker?} the
# deterministic layer can cross-check, or None when unavailable. NEVER raises
# out of extract(): any failure degrades to deterministic-only.
# ---------------------------------------------------------------------------
_HAIKU_MODEL = "claude-haiku-4-5"


def _haiku_available():
    """(client, True) if a usable Messages-capable SDK + key exist, else (None, False)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, False
    try:
        import anthropic  # noqa
    except Exception:
        return None, False
    try:
        client = anthropic.Anthropic()
    except Exception:
        return None, False
    # The 0.7.x SDK has no .messages; require it before attempting a call.
    if not hasattr(client, "messages"):
        return None, False
    return client, True


def haiku_refine(doc_text: str, channel: str) -> Optional[dict]:
    """Cached Haiku extraction. Returns parsed JSON dict or None.

    Cache hit -> returns cached JSON without any network call (determinism).
    No key / old SDK / any error -> None (caller stays deterministic).
    """
    os.makedirs(_LLM_CACHE_DIR, exist_ok=True)
    key = _doc_sha1(_HAIKU_MODEL, doc_text)
    cache_path = os.path.join(_LLM_CACHE_DIR, key + ".json")
    if os.path.exists(cache_path):
        try:
            return json.load(open(cache_path))
        except Exception:
            pass  # corrupt cache -> recompute / degrade

    client, ok = _haiku_available()
    if not ok:
        return None

    prompt = (
        "Extract every PUBLICLY-TRADED COMPANY explicitly named in the text "
        "below and the THEMES it touches. Return ONLY minified JSON of shape "
        '{"companies":[{"name":..,"ticker":..,"is_person_only":bool,'
        '"quote":..}],"themes":[..]}. '
        '"quote" MUST be a verbatim substring of the text. Mark is_person_only '
        "true when the name refers to a PERSON (e.g. 'Michael Dell') rather "
        "than the company. Themes from this set only: "
        + ", ".join(THEME_MATERIALITY) + ".\n\nTEXT:\n" + doc_text[:6000]
    )
    try:
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0) if m else raw)
        with open(cache_path, "w") as f:
            json.dump(data, f, indent=2)
        return data
    except Exception:
        return None  # any failure -> deterministic only


# ---------------------------------------------------------------------------
# Per-mention judgment helpers (deterministic).
# ---------------------------------------------------------------------------
def _window(text: str, span, n: int = 80) -> str:
    lo = max(0, span[0] - n)
    hi = min(len(text), span[1] + n)
    return text[lo:span[0]] + " " + text[span[1]:hi]


def _is_person_mention(text: str, m: Match) -> bool:
    """True if THIS surface mention reads as a person, not the company.

    A capitalized given name (optionally two, or 'X and Y') immediately
    precedes the matched word, OR the word is possessive ("Dell's", "Dells").
    Only meaningful for single-word brand names whose distinctive name could be
    a surname (Dell, Ford). Multi-word company names (Dell Technologies) are
    never person mentions.
    """
    if " " in m.name.strip():
        return False
    before = text[max(0, m.span[0] - 40):m.span[0]]
    if PERSON_BEFORE.search(before) or PERSON_AND.search(before):
        return True
    # possessive plural / singular surname: "the Dells", "Dell's gift"
    after = text[m.span[1]:m.span[1] + 3]
    if after[:2] == "'s" or (m.name.endswith("s") and before.rstrip().endswith("the")):
        # "the Dells" style -> family/person
        if re.search(r"\bthe\s*$", before):
            return True
    return False


def _context_sharpness(text: str, m: Match) -> float:
    """0..1 sharpness of the company sense around a mention."""
    win = _window(text, m.span)
    sharp = bool(SHARP_COMPANY_CUE.search(win))
    commercial = bool(COMMERCIAL_PRODUCT_CUE.search(win))
    if m.name.strip().count(" ") >= 1:
        # Multi-word distinctive company name is itself a strong signal.
        return 1.0 if sharp else 0.85
    if sharp:
        return 0.9
    if commercial:
        return 0.7
    # bare single brand word, no cue
    return 0.45


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------
def extract(
    doc_text: str,
    channel: str = "social_post",
    *,
    freq_weight: float = 1.0,
    keep_threshold: float = 0.35,
    use_llm: bool = True,
) -> dict:
    """Hybrid extraction. See module docstring for the return contract.

    freq_weight : optional 0..1 corpus-frequency factor the generator may pass
                  (a name said in many docs is more trustworthy). Default 1.0
                  keeps single-doc behavior pure.
    keep_threshold : confidence floor; below it -> logged discard.
    use_llm     : allow the cached Haiku cross-check if available.
    """
    text = doc_text or ""
    themes = classify_themes(text)
    backend = "deterministic"

    # Optional cached Haiku refine (cross-check only; deterministic decides).
    llm = haiku_refine(text, channel) if use_llm else None
    if llm is not None:
        backend = "haiku-4-5+deterministic"
    llm_tickers = set()
    if isinstance(llm, dict):
        for c in llm.get("companies", []) or []:
            t = (c.get("ticker") or "").upper().strip()
            if t:
                llm_tickers.add(t)

    R = get_resolver()
    matches = R.find_in_text(text)

    ch_w = CHANNEL_WEIGHT.get(channel, 0.5)
    kept = {}        # ticker -> best company record
    discards = []    # list of {name, ticker, reason}

    for m in matches:
        name = m.name
        ticker = m.ticker
        low = name.lower().strip()

        # ---- HARD verification gate (DESIGN #5), re-asserted in code -------
        # (a) quote literal-substring of doc, (b) ticker resolves in gazetteer.
        quote = text[m.span[0]:m.span[1]]
        if quote != name or name not in text:
            discards.append({"name": name, "ticker": ticker,
                             "reason": "quote not a literal substring of doc"})
            continue
        if R.resolve_name(m.distinctive) is None and ticker not in {
            g["ticker"] for g in R.gazetteer
        }:
            discards.append({"name": name, "ticker": ticker,
                             "reason": "ticker does not resolve in gazetteer"})
            continue

        # ---- attack-target / media blocklist (hard drop) ------------------
        if ticker in ATTACK_BLOCKLIST:
            discards.append({"name": name, "ticker": ticker,
                             "reason": f"attack-target/media blocklist ({ATTACK_BLOCKLIST[ticker]})"})
            continue

        # ---- unit/number/filler word gate ---------------------------------
        if low in UNIT_NUMBER_GATE:
            discards.append({"name": name, "ticker": ticker,
                             "reason": "unit/number/common filler word, not a company"})
            continue

        # ---- pure acronym / theme-token gate ------------------------------
        if low in ACRONYM_THEME_GATE:
            discards.append({"name": name, "ticker": ticker,
                             "reason": "acronym/theme token (concept, not a company)"})
            continue

        # ---- bare gated word immediately followed by a corporate descriptor
        # ("United Technologies", "American Industries"): the speaker named a
        # longer company than the bare-word ticker -> bare ticker is wrong.
        if " " not in name.strip() and R.is_ambiguous(name):
            after = text[m.span[1]:m.span[1] + 24]
            if CORP_DESCRIPTOR_AFTER.match(after):
                discards.append({"name": name, "ticker": ticker,
                                 "reason": "bare word is head of a longer company name (gazetteer split)"})
                continue

        # ---- ambiguous (gated) single-word names: need context ------------
        if m.ambiguous and not m.context_ok:
            discards.append({"name": name, "ticker": ticker,
                             "reason": "ambiguous common word, no company context nearby"})
            continue

        win = _window(text, m.span)

        # ---- multi-word match must be properly Title-Cased in the source --
        # A genuine multi-word company name appears Title-Cased ("Texas
        # Instruments", "Bloom Energy"). A lowercase interior word means the
        # match is a common-phrase fragment that merely collides with a
        # gazetteer name ("American national security" -> American National).
        toks_surface = name.split()
        if len(toks_surface) > 1:
            _connectors = {"and", "of", "the", "for", "&"}
            titled = all(t[:1].isupper() or t.lower() in _connectors
                         for t in toks_surface)
            if not titled:
                discards.append({"name": name, "ticker": ticker,
                                 "reason": "multi-word match not title-cased (phrase fragment, not company)"})
                continue

        # ---- TIGHT gate for low-distinctiveness names ---------------------
        # The resolver's context_ok is a loose 60-char any-cue test. In a
        # finance-dense doc that is satisfied by almost everything, so a gated
        # common word ("First", "News", "here", "track", "United", "Global")
        # -- or a multi-word name whose every token is itself a common word
        # ("American national") -- would otherwise surface a microcap. Require
        # instead that the surface is (a) used as a Proper noun (capitalized)
        # AND (b) sits TIGHTLY adjacent (<= STRICT_WINDOW) to a company-
        # STRUCTURAL cue. Genuinely distinctive multi-word names are exempt.
        toks = name.split()
        all_common = bool(toks) and all(R.is_ambiguous(t) for t in toks)
        low_distinctiveness = m.ambiguous or (len(toks) > 1 and all_common)
        if low_distinctiveness:
            tight = _window(text, m.span, STRICT_WINDOW)
            capitalized = name[:1].isupper()
            if not (capitalized and STRICT_BRAND_CUE.search(tight)):
                discards.append({"name": name, "ticker": ticker,
                                 "reason": "low-distinctiveness name w/o tight proper-noun company cue"})
                continue

        # ---- soft-brand common-noun gate ----------------------------------
        # brand-override word that is also an ordinary noun -> require SHARP cue.
        if low in SOFT_BRAND_COMMON_NOUN and not SHARP_COMPANY_CUE.search(win):
            discards.append({"name": name, "ticker": ticker,
                             "reason": "brand word in common-noun sense (no sharp company cue)"})
            continue

        # ---- is_person_only (DESIGN #4) -----------------------------------
        is_person = _is_person_mention(text, m)
        has_company_action = bool(SHARP_COMPANY_CUE.search(win)) or bool(
            COMMERCIAL_PRODUCT_CUE.search(win)
        )
        # Multi-word company name OR a company action nearby overrides person.
        person_only_drop = is_person and not has_company_action

        # ---- confidence blend ---------------------------------------------
        #   context_sharpness x frequency x channel x gazetteer_agreement
        sharp = _context_sharpness(text, m)
        gaz_agree = 1.0  # resolver hit == gazetteer agreement by construction
        if ticker in llm_tickers:
            gaz_agree = min(1.0, gaz_agree + 0.0)  # LLM concurs (already max)
        elif llm is not None:
            gaz_agree = 0.9  # LLM ran and did NOT list it -> slight discount
        confidence = round(sharp * freq_weight * ch_w * gaz_agree, 3)

        # ---- decision -----------------------------------------------------
        if person_only_drop:
            # Record as a person-only discard, BUT keep the record around in
            # case a later mention of the same ticker in this doc has company
            # action (handled by the kept[] merge below: person-only never
            # overwrites a kept company record).
            discards.append({"name": name, "ticker": ticker,
                             "reason": "is_person_only (bare person name, no company action)"})
            continue

        if confidence < keep_threshold:
            discards.append({"name": name, "ticker": ticker,
                             "reason": f"confidence {confidence} < threshold {keep_threshold}"})
            continue

        rec = {
            "name": name,
            "ticker": ticker,
            "confidence": confidence,
            "quote": quote,
            "is_person_only": is_person,  # mention-level flag (kept anyway)
        }
        prev = kept.get(ticker)
        if prev is None or rec["confidence"] > prev["confidence"]:
            kept[ticker] = rec

    # A ticker that was kept via one mention should not also appear as a
    # person-only discard; prune those for a clean audit.
    kept_tickers = set(kept)
    discards = [d for d in discards if not (
        d["ticker"] in kept_tickers
        and d["reason"].startswith("is_person_only")
    )]

    return {
        "themes": themes,
        "companies": sorted(kept.values(), key=lambda r: -r["confidence"]),
        "discards": discards,
        "backend": backend,
    }


# ---------------------------------------------------------------------------
# Tiny self-test when run directly.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cases = [
        ("go out and buy a Dell", "social_post"),
        ("Great job by Michael and Susan Dell on InvestAmerica.org", "social_praise"),
        ("FBI gives Congress intel on alleged China plot", "social_post"),
        ("APPLE HAS JUST ANNOUNCED A RECORD 500 BILLION DOLLAR INVESTMENT", "social_post"),
        ("Jay Powell and the Fed failed to stop Inflation; lower Interest Rates", "social_post"),
    ]
    for txt, ch in cases:
        r = extract(txt, ch)
        print("\n>>", repr(txt[:60]), "| channel:", ch, "| backend:", r["backend"])
        print("   themes:", r["themes"])
        for c in r["companies"]:
            print(f"   KEEP {c['ticker']:6} conf={c['confidence']} person={c['is_person_only']} q={c['quote']!r}")
        for d in r["discards"]:
            print(f"   DROP {str(d['ticker']):6} {d['name']!r} -> {d['reason']}")
