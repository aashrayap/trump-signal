#!/usr/bin/env python3
"""Build a curated investable universe (the matching gazetteer) + attack blocklist.

Narrows the full SEC gazetteer (~10k US filers) down to ~190 thesis-relevant,
decent-market-cap names (semis + software heavy, with energy / finance / defense /
pharma / crypto / auto adjacents that Trump actually talks about). This is what
kills the microcap / common-word noise (MIAX, PSO, "on"->ONON, "billion"->BGHL...)
and lets names like DELL resurface instead of ranking #50.

Reuses the SEC-built {title, distinctive, aliases} where the ticker exists so the
resolver's matching quality is preserved; adds manual entries for foreign listings.

Run:  python3 build_universe.py     (idempotent; backs up the full gazetteer once)
"""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
FULL_BAK = os.path.join(HERE, "gazetteer_full.bak.json")
GAZ = os.path.join(HERE, "gazetteer.json")
BLOCK = os.path.join(HERE, "attack_blocklist.json")

# 1. back up the full gazetteer ONCE, then always read the full set from the backup
if not os.path.exists(FULL_BAK):
    shutil.copy(GAZ, FULL_BAK)
full = json.load(open(FULL_BAK))
by_ticker = {}
for g in full:
    by_ticker.setdefault(g["ticker"].upper(), g)

# 2. curated universe:  ticker -> preferred display / distinctive match name
CURATED = {
    # --- semiconductors & equipment (thesis core) ---
    "NVDA": "NVIDIA", "AMD": "Advanced Micro Devices", "INTC": "Intel",
    "TSM": "Taiwan Semiconductor Manufacturing", "AVGO": "Broadcom", "QCOM": "Qualcomm",
    "MU": "Micron", "TXN": "Texas Instruments", "ADI": "Analog Devices",
    "NXPI": "NXP Semiconductors", "MCHP": "Microchip Technology", "ON": "onsemi",
    "MPWR": "Monolithic Power Systems", "SWKS": "Skyworks Solutions", "QRVO": "Qorvo",
    "LSCC": "Lattice Semiconductor", "SLAB": "Silicon Laboratories", "AMAT": "Applied Materials",
    "LRCX": "Lam Research", "KLAC": "KLA", "ASML": "ASML", "TER": "Teradyne", "ENTG": "Entegris",
    "ARM": "Arm Holdings", "MRVL": "Marvell Technology", "GFS": "GlobalFoundries",
    "STM": "STMicroelectronics", "UMC": "United Microelectronics", "WOLF": "Wolfspeed",
    "POWI": "Power Integrations", "SMTC": "Semtech", "ALAB": "Astera Labs", "CRDO": "Credo Technology",
    "COHR": "Coherent", "LITE": "Lumentum", "RMBS": "Rambus", "SNDK": "SanDisk", "KXIAY": "Kioxia",
    "AIXA": "AIXTRON", "005930.KS": "Samsung Electronics", "000660.KS": "SK hynix", "NVMI": "Nova",
    "ACLS": "Axcelis Technologies", "ONTO": "Onto Innovation", "FORM": "FormFactor",
    "AEIS": "Advanced Energy Industries", "KLIC": "Kulicke and Soffa", "SITM": "SiTime",
    "MTSI": "MACOM", "INDI": "indie Semiconductor",
    # --- software / cybersecurity / data / apps ---
    "MSFT": "Microsoft", "ORCL": "Oracle", "CRM": "Salesforce", "ADBE": "Adobe", "NOW": "ServiceNow",
    "SAP": "SAP", "IBM": "IBM", "INTU": "Intuit", "PLTR": "Palantir", "SNOW": "Snowflake",
    "CRWD": "CrowdStrike", "PANW": "Palo Alto Networks", "FTNT": "Fortinet", "ZS": "Zscaler",
    "S": "SentinelOne", "NET": "Cloudflare", "DDOG": "Datadog", "MDB": "MongoDB", "CFLT": "Confluent",
    "GTLB": "GitLab", "TEAM": "Atlassian", "WDAY": "Workday", "ADSK": "Autodesk", "ANSS": "Ansys",
    "CDNS": "Cadence Design Systems", "SNPS": "Synopsys", "ROP": "Roper Technologies", "PTC": "PTC",
    "HUBS": "HubSpot", "DOCU": "DocuSign", "OKTA": "Okta", "TWLO": "Twilio", "ESTC": "Elastic",
    "DT": "Dynatrace", "PATH": "UiPath", "AI": "C3.ai", "U": "Unity Software", "RBLX": "Roblox",
    "APP": "AppLovin", "NFLX": "Netflix", "FICO": "Fair Isaac", "ZM": "Zoom", "GWRE": "Guidewire",
    "MNDY": "monday.com", "FROG": "JFrog", "TTD": "The Trade Desk", "SHOP": "Shopify",
    "UBER": "Uber", "ABNB": "Airbnb", "XYZ": "Block", "AXON": "Axon Enterprise",
    # --- mega-cap platforms ---
    "AAPL": "Apple", "GOOGL": "Alphabet", "AMZN": "Amazon", "META": "Meta Platforms",
    # --- infra / hardware / networking / datacenter / AI power ---
    "DELL": "Dell Technologies", "HPE": "Hewlett Packard Enterprise", "SMCI": "Super Micro Computer",
    "ANET": "Arista Networks", "CSCO": "Cisco", "JNPR": "Juniper Networks", "NTAP": "NetApp",
    "WDC": "Western Digital", "STX": "Seagate", "PSTG": "Pure Storage", "CIEN": "Ciena", "NOK": "Nokia",
    "ERIC": "Ericsson", "GLW": "Corning", "VRT": "Vertiv", "NBIS": "Nebius", "CRWV": "CoreWeave",
    "CORZ": "Core Scientific", "CLSK": "CleanSpark", "WYFI": "WhiteFiber", "PENG": "Penguin Solutions",
    "HPQ": "HP",
    # --- energy / power / utilities ---
    "XOM": "Exxon Mobil", "CVX": "Chevron", "COP": "ConocoPhillips", "EOG": "EOG Resources",
    "SLB": "Schlumberger", "OXY": "Occidental Petroleum", "KMI": "Kinder Morgan", "WMB": "Williams",
    "LNG": "Cheniere Energy", "NEE": "NextEra Energy", "DUK": "Duke Energy", "SO": "Southern Company",
    "VST": "Vistra", "CEG": "Constellation Energy", "GEV": "GE Vernova", "NRG": "NRG Energy",
    "BE": "Bloom Energy", "FCEL": "FuelCell Energy", "FSLR": "First Solar", "ENPH": "Enphase Energy",
    "RUN": "Sunrun", "PLUG": "Plug Power",
    # --- AI-datacenter power / nuclear (the AI-infra power bottleneck + energy-dominance theme) ---
    "OKLO": "Oklo", "SMR": "NuScale Power", "CCJ": "Cameco", "TLN": "Talen Energy",
    # --- finance ---
    "JPM": "JPMorgan Chase", "BAC": "Bank of America", "GS": "Goldman Sachs", "MS": "Morgan Stanley",
    "BLK": "BlackRock", "BX": "Blackstone", "KKR": "KKR", "SCHW": "Charles Schwab", "V": "Visa",
    "MA": "Mastercard", "AXP": "American Express", "PYPL": "PayPal", "COIN": "Coinbase",
    "SPGI": "S&P Global", "NDAQ": "Nasdaq",
    # --- defense / aerospace / industrial ---
    "LMT": "Lockheed Martin", "RTX": "RTX", "NOC": "Northrop Grumman", "GD": "General Dynamics",
    "BA": "Boeing", "LHX": "L3Harris Technologies", "HII": "Huntington Ingalls", "GE": "GE Aerospace",
    "HON": "Honeywell", "CAT": "Caterpillar", "ETN": "Eaton", "EMR": "Emerson Electric",
    # --- pharma / health ---
    "LLY": "Eli Lilly", "PFE": "Pfizer", "MRK": "Merck", "JNJ": "Johnson & Johnson", "ABBV": "AbbVie",
    "BMY": "Bristol-Myers Squibb", "AMGN": "Amgen", "GILD": "Gilead Sciences", "MRNA": "Moderna",
    "NVO": "Novo Nordisk", "AZN": "AstraZeneca", "NVS": "Novartis", "REGN": "Regeneron",
    "VRTX": "Vertex Pharmaceuticals",
    # --- crypto-adjacent ---
    "MSTR": "MicroStrategy", "RIOT": "Riot Platforms", "MARA": "MARA Holdings", "HOOD": "Robinhood",
    "BKKT": "Bakkt",
    # --- autos / EV ---
    "TSLA": "Tesla", "F": "Ford", "GM": "General Motors", "RIVN": "Rivian",
}

# 3. attack-target / media / political-noise blocklist (hard-dropped even if matched)
BLOCKLIST = {
    "NYT": "New York Times - media/attack target, not an investment signal",
    "NWSA": "News Corp - media", "NWS": "News Corp - media",
    "FOXA": "Fox - media", "FOX": "Fox - media",
    "CMCSA": "Comcast/NBC/MSNBC - media", "WBD": "Warner Bros Discovery/CNN - media",
    "PARA": "Paramount/CBS - media", "PARAA": "Paramount/CBS - media",
    "TGNA": "Tegna - local media", "GCI": "Gannett - media", "SSP": "Scripps - media",
    "NXST": "Nexstar - media", "DJT": "Trump Media - self-referential noise",
}

# 4. build curated gazetteer, reusing the SEC entry where the ticker exists
out, missing, seen = [], [], set()
for tk, name in CURATED.items():
    if tk in BLOCKLIST or tk in seen:
        continue
    seen.add(tk)
    src = by_ticker.get(tk.upper())
    if src:
        e = dict(src)
        e["distinctive"] = name                       # prefer our clean display name
        al = set(e.get("aliases") or [])
        al.add(name)
        al.add(src.get("title", ""))
        e["aliases"] = sorted(a for a in al if a)
        out.append(e)
    else:
        missing.append(tk)
        out.append({"ticker": tk, "title": name, "distinctive": name,
                    "aliases": [name], "source": "manual_curated"})

json.dump(out, open(GAZ, "w"), indent=1)
json.dump(BLOCKLIST, open(BLOCK, "w"), indent=1)

# 5. report + coverage check against the 32 thesis tickers
thesis32 = {"AIXA", "ALAB", "AMZN", "ARM", "ASML", "BE", "CIEN", "CLSK", "CORZ", "CRWD", "CRWV",
            "DELL", "FCEL", "FICO", "INTC", "KXIAY", "MU", "NBIS", "NOK", "ON", "PANW", "PENG",
            "POWI", "RBLX", "005930.KS", "000660.KS", "SMTC", "SNDK", "TSM", "TXN", "WOLF", "WYFI"}
have = {e["ticker"] for e in out}
miss32 = sorted(thesis32 - have)
print(f"curated universe: {len(out)} names  (narrowed from {len(full)})")
print(f"attack blocklist: {len(BLOCKLIST)} tickers")
print(f"added manual (not in SEC set): {len(missing)} -> {sorted(missing)}")
print(f"thesis-32 coverage: {32 - len(miss32)}/32" + (f"  MISSING: {miss32}" if miss32 else "  (all present)"))
