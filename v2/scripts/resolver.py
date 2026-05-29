#!/usr/bin/env python3
"""
Deterministic company name -> ticker resolver (GAZETTEER lane output).

Pure stdlib. Loads:
  - gazetteer.json           (built by build_gazetteer.py)
  - ambiguity_blocklist.json

Matching rules (see resolver_spec.md):
  * Index every distinctive name + alias + full title, case-insensitively.
  * Longest distinctive name first (greedy): "Texas Instruments" beats "Texas".
  * Word-boundary, case-insensitive. No substring-in-the-middle hits.
  * Multi-word distinctive names match freely (low collision risk).
  * Single common-word names on the ambiguity blocklist are EXCLUDED from the
    bare-word index. They resolve ONLY through resolve(text, context=...) when a
    commercial / product / company cue sits nearby.

Public API:
  R = Resolver()                         # loads json next to this file
  R.resolve_name("Nvidia")               -> "NVDA" | None
  R.is_ambiguous("Texas")                -> True
  R.find_in_text("go out and buy a Dell")-> [Match(...), ...]   (greedy, deduped)

Match fields: name (surface span text), ticker, title, span (start,end),
              distinctive (the gazetteer name matched), ambiguous (bool),
              context_ok (bool, only meaningful when ambiguous).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_GAZ = os.path.join(_HERE, "gazetteer.json")
_BLOCK = os.path.join(_HERE, "ambiguity_blocklist.json")
_GATE = os.path.join(_HERE, "common_word_gate.json")

# Commercial / product / company context cues used to UNLOCK an ambiguous
# single-word company name when it appears near one of these.
CONTEXT_CUES = re.compile(
    r"\b("
    r"stocks?|shares?|ticker|nasdaq|nyse|earnings|ipo|market[- ]?cap|"
    r"ceo|cfo|chips?|semiconductors?|semis|compan(?:y|ies)|corp|corporation|"
    r"inc\.?|holdings?|technolog(?:y|ies)|makers?|manufactur(?:e|es|er|ers|ing)|"
    r"buy(?:ing|s)?|bought|sell(?:ing|s)?|sold|invest(?:ing|ed|ment|ments|or|ors)?|"
    r"products?|devices?|phones?|laptops?|computers?|servers?|gpus?|"
    r"factor(?:y|ies)|plants?|fabs?|deals?|mergers?|acquire[ds]?|acquisitions?|billions?|"
    r"share price|valuation|dividends?|founders?|headquarter(?:s|ed)?"
    r")\b",
    re.IGNORECASE,
)

# How many characters around an ambiguous hit we scan for a context cue.
CONTEXT_WINDOW = 60

# Single tokens shorter than this are always gated (e.g. "AI", "IT", "ON").
_MIN_SINGLE_LEN = 4

# Minimal fallback common-word gate used ONLY if common_word_gate.json is missing.
# The authoritative gate is built from the system dictionary by build_gazetteer.py
# and frozen to common_word_gate.json. Keep this small; it is a safety net, not the
# source of truth.
_FALLBACK_GATE = {
    "the", "and", "for", "you", "all", "now", "new", "post", "work", "news", "bill",
    "complete", "hope", "forward", "leader", "team", "good", "first", "world", "next",
    "open", "core", "bloom", "power", "fair", "white", "clean", "general", "american",
    "apple", "texas", "arm", "gap", "match", "block", "square", "on", "be", "here",
    "washington", "california", "mexico", "powell", "energy", "gold", "golden",
}


@dataclass
class Match:
    name: str
    ticker: str
    title: str
    span: tuple
    distinctive: str
    ambiguous: bool
    context_ok: bool

    def as_dict(self):
        d = asdict(self)
        d["span"] = list(self.span)
        return d


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _prefer_ticker(a: str, b: str) -> str:
    """Pick the more canonical of two tickers for the SAME company name.

    Prefers the base/common share class: no hyphen beats hyphenated (F over F-PB),
    then shorter, then alphabetical. Deterministic.
    """
    def rank(t):
        return ("-" in t, len(t), t)
    return a if rank(a) <= rank(b) else b


class Resolver:
    def __init__(self, gaz_path: str = _GAZ, block_path: str = _BLOCK, gate_path: str = _GATE):
        gaz = json.load(open(gaz_path))
        block = json.load(open(block_path))
        self.gazetteer = gaz
        self.ambiguity = {k.lower(): v for k, v in block.items()}

        # Frozen common-word gate (built from system dictionary at build time). Falls
        # back to a small embedded set if the file is absent so the resolver still runs.
        if os.path.exists(gate_path):
            gp = json.load(open(gate_path))
            self.gate = set(gp.get("gated", []))
            self.brand_override = set(gp.get("brand_override", []))
        else:
            self.gate = set(_FALLBACK_GATE)
            self.brand_override = set()

        # name(lower) -> {ticker, title, distinctive}
        self.index = {}
        # ambiguous single-word names get their own gated index (not in self.index)
        self.ambiguous_index = {}

        for g in gaz:
            ticker = g["ticker"]
            title = g["title"]
            distinctive = g.get("distinctive") or title
            surfaces = {title, distinctive}
            surfaces.update(g.get("aliases", []) or [])
            for surf in surfaces:
                key = _norm(surf)
                if not key:
                    continue
                is_single = " " not in key
                payload = {"ticker": ticker, "title": title, "distinctive": distinctive}
                # Gate single-token names that are on the hand blocklist OR are common
                # English words / too short. These resolve ONLY with nearby context.
                if is_single and (key in self.ambiguity or self._gated_single(key)):
                    cur = self.ambiguous_index.get(key)
                    if cur is None or _prefer_ticker(ticker, cur["ticker"]) == ticker:
                        self.ambiguous_index[key] = payload
                    continue
                # Prefer the canonical owner: an entry whose distinctive name equals this
                # surface, else the base (hyphen-less / shorter) ticker among share classes.
                cur = self.index.get(key)
                if cur is None:
                    self.index[key] = payload
                elif _norm(distinctive) == key and _norm(cur["distinctive"]) != key:
                    self.index[key] = payload
                elif _norm(cur["distinctive"]) != key and _prefer_ticker(ticker, cur["ticker"]) == ticker:
                    self.index[key] = payload

        # Precompile a single alternation regex, longest-name-first, for the clean
        # (non-ambiguous) index. Word boundaries on both sides.
        names = sorted(self.index.keys(), key=len, reverse=True)
        if names:
            alt = "|".join(re.escape(n) for n in names)
            self._clean_re = re.compile(r"(?<![\w&])(" + alt + r")(?![\w&])", re.IGNORECASE)
        else:
            self._clean_re = None

        # Ambiguous alternation (single words) compiled separately.
        anames = sorted(self.ambiguous_index.keys(), key=len, reverse=True)
        if anames:
            aalt = "|".join(re.escape(n) for n in anames)
            self._amb_re = re.compile(r"(?<![\w&])(" + aalt + r")(?![\w&])", re.IGNORECASE)
        else:
            self._amb_re = None

    def _gated_single(self, token_lower: str) -> bool:
        """True when a single-token name must be context-gated (frozen gate / too short),
        unless it is on the brand override (e.g. Dell, Micron)."""
        if " " in token_lower:
            return False
        if token_lower in self.brand_override:
            return False
        if len(token_lower) < _MIN_SINGLE_LEN:
            return True
        return token_lower in self.gate

    # ---- simple lookups -------------------------------------------------
    def is_ambiguous(self, name: str) -> bool:
        """True if name is gated: on the hand blocklist, in the frozen common-word gate,
        or a known gated single-token company name. Such names never bare-word match."""
        key = _norm(name)
        if key in self.ambiguity:
            return True
        if key in self.ambiguous_index:
            return True
        return self._gated_single(key)

    def ambiguity_reason(self, name: str) -> Optional[str]:
        key = _norm(name)
        if key in self.ambiguity:
            return self.ambiguity[key]
        if self._gated_single(key) or key in self.ambiguous_index:
            if len(key) < _MIN_SINGLE_LEN:
                return "single token shorter than 4 chars; context required"
            return "common English word / place / person name; context required"
        return None

    def resolve_name(self, name: str, context: str = "") -> Optional[str]:
        """Resolve a bare name to a ticker.

        Non-ambiguous names resolve directly. Ambiguous single-word names resolve
        only if `context` contains a commercial/product/company cue.
        """
        key = _norm(name)
        if key in self.index:
            return self.index[key]["ticker"]
        if key in self.ambiguous_index:
            if context and CONTEXT_CUES.search(context):
                return self.ambiguous_index[key]["ticker"]
            return None
        return None

    # ---- text scanning --------------------------------------------------
    def find_in_text(self, text: str, include_ambiguous: bool = True):
        """Greedy, longest-first, non-overlapping company hits in free text.

        Ambiguous single-word hits are included only when a context cue sits within
        CONTEXT_WINDOW chars; each carries ambiguous=True / context_ok flag.
        """
        if not text:
            return []
        spans = []  # (start, end, Match)

        if self._clean_re is not None:
            for m in self._clean_re.finditer(text):
                surf = m.group(1)
                payload = self.index[_norm(surf)]
                spans.append((m.start(1), m.end(1), Match(
                    name=surf, ticker=payload["ticker"], title=payload["title"],
                    span=(m.start(1), m.end(1)), distinctive=payload["distinctive"],
                    ambiguous=False, context_ok=True,
                )))

        if include_ambiguous and self._amb_re is not None:
            for m in self._amb_re.finditer(text):
                surf = m.group(1)
                payload = self.ambiguous_index[_norm(surf)]
                lo = max(0, m.start(1) - CONTEXT_WINDOW)
                hi = min(len(text), m.end(1) + CONTEXT_WINDOW)
                window = text[lo:m.start(1)] + " " + text[m.end(1):hi]
                ok = bool(CONTEXT_CUES.search(window))
                spans.append((m.start(1), m.end(1), Match(
                    name=surf, ticker=payload["ticker"], title=payload["title"],
                    span=(m.start(1), m.end(1)), distinctive=payload["distinctive"],
                    ambiguous=True, context_ok=ok,
                )))

        # Resolve overlaps: keep the longest span; on tie, prefer non-ambiguous.
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0]), s[2].ambiguous))
        chosen = []
        last_end = -1
        for lo, hi, mt in spans:
            if lo >= last_end:
                chosen.append(mt)
                last_end = hi
        return chosen


# Module-level singleton for cheap reuse by the generator.
_DEFAULT = None


def get_resolver() -> Resolver:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Resolver()
    return _DEFAULT


if __name__ == "__main__":
    import sys
    R = Resolver()
    print(f"clean index size: {len(R.index)}  ambiguous gated: {len(R.ambiguous_index)}")
    for q in sys.argv[1:] or ["Nvidia", "Dell", "Micron", "CoreWeave", "Texas", "Arm", "Apple"]:
        print(f"resolve_name({q!r}) = {R.resolve_name(q)}  ambiguous={R.is_ambiguous(q)}")
