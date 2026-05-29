# Trump Truth Social → Market Alpha — Prototype Import (provenance)

Local working copy. Ignored by git (repo-root `/tmp/`). Do not commit.

## Source
Copied 2026-05-29 from the local Codex CLI run:
`/Users/ash/Documents/Codex/2026-05-25/are-yo-able-to-scrape-tweets/`

Original scope: Trump's Truth posts from 2025-01-20 onward; event study vs daily market bars.

## Files copied (byte-identical to source)

| File | Rows | What it is |
| --- | --- | --- |
| `output/posts_all.csv` | 9,287 | All ingested Trump's Truth posts |
| `output/claims.csv` | 2,085 | Market-relevant claim candidates (event_id, timestamps, topic, tickers, url, is_repost/deleted, confidence) |
| `output/event_returns.csv` | 11,641 | Per-claim × asset return rows across event windows |
| `output/topic_summary.csv` | 114 | Topic/ticker bucket aggregates (n, abnormal return, p-values) |
| `output/top_event_moves.csv` | 25 | Largest individual event moves |
| `output/asset_summary.csv` | 23 | Per-asset rollups |
| `output/market_prices.csv` | 8,047 | Daily bars used |
| `output/trumpfm_crosscheck.csv` | 60 | trump.fm cross-check sample (60/60 matched) |
| `output/prototype_report.md` | — | Codex's own writeup + caveats |
| `scripts/prototype_event_study.py` | — | The pipeline (methodology) |

Row counts reconcile to `prototype_report.md` (9,287 posts / 2,085 claims / 11,641 return rows / 60 cross-checks).

## NOT copied (still at source)
`data/raw/` (~28 MB): trumpstruth RSS XML + nasdaq/coinbase market JSON. Needed only to re-run the pipeline. Copy on demand.

## Known caveats (from the run)
- No alpha established yet: small N per signal, April-2025 shock clustering, no multiple-testing correction.
- FRED timed out -> FRED mirrors omitted.
