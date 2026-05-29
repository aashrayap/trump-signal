# Prototype Report - Trump Truth Social Market Event Study

> **Historical document — the killed v1 event-study prototype.** Paths below
> (the `output/` tree) describe that prototype's own layout, **not** this repo.
> In the current flat layout, inputs live under `data/` and generated artifacts
> under `outputs/`. See the root `README.md` for the live layout.

Run date: 2026-05-25T16:33:40.346877+00:00
Scope: 2025-01-20 through 2026-05-25

## Data Loaded
- Trump's Truth posts parsed: 9,287
- Market-relevant claim candidates: 2,085
- Original non-deleted claim candidates used for event study: 1,920
- trump.fm sampled cross-checks: 60/60 found
- Market daily price rows: 8,047
- Event/asset return rows: 11,641

## Quick Thesis Read
- Buckets with largest absolute mean beta-adjusted same-session/next-session move:
- company_specific / TSLA: n=6, mean raw=3.09%, mean abnormal=4.80%, hit=83%, random-p=0.043
- company_specific / NVDA: n=6, mean raw=1.38%, mean abnormal=2.85%, hit=67%, random-p=0.210
- trade_tariffs / TSLA: n=5, mean raw=-1.73%, mean abnormal=-1.95%, hit=20%, random-p=0.255
- trade_tariffs / INTC: n=5, mean raw=-1.72%, mean abnormal=-1.91%, hit=40%, random-p=0.362
- trade_tariffs / PFE: n=8, mean raw=1.52%, mean abnormal=1.31%, hit=88%, random-p=0.007
- trade_tariffs / NVDA: n=5, mean raw=-0.96%, mean abnormal=-1.16%, hit=20%, random-p=0.406
- semis_ai_tech / USO: n=10, mean raw=-0.86%, mean abnormal=-0.88%, hit=30%, random-p=0.283
- company_specific / AAPL: n=6, mean raw=-1.76%, mean abnormal=-0.80%, hit=33%, random-p=0.035

## Important Caveats
- Daily bars only for most assets; no intraday execution claim here.
- Keyword filters identify candidate claims, not verified causal news shocks.
- Multiple testing is not yet corrected beyond reporting random-date p-values.
- Direct Truth Social scraping was avoided; archive provenance retained in CSV outputs.

## Output Files (v1 prototype layout — historical, not this repo)
- `output/posts_all.csv` → now `data/posts_all.csv`
- `output/claims.csv` (v1 only; not regenerated in current pipeline)
- `output/trumpfm_crosscheck.csv` (v1 only)
- `output/market_prices.csv` → now `data/market_prices.csv`
- `output/event_returns.csv` (v1 only)
- `output/topic_summary.csv` (v1 only)
- `output/asset_summary.csv` (v1 only)
- `output/top_event_moves.csv` (v1 only)
