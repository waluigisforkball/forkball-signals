# Forkball Signals

A **completely free** scheduled market tripwire. It watches your positions,
watchlist, and catalyst themes; when something trips a rule (a price move, a
catalyst keyword, a level cross) it pushes you a Discord alert and posts it to a
dashboard. Each alert carries a **ready-to-paste block** — tap copy, paste into
Claude in the app, and your `mike-market-analyst` skill does the analysis for free.

**The scanner finds. You analyze. No AI tokens, no API bill.**

## Why this costs $0

| Piece | Service | Cost |
|---|---|---|
| Scheduler | GitHub Actions (cron) | Free |
| Quotes + news | Finnhub free tier | Free |
| Dashboard host | GitHub Pages | Free |
| Alerts | Discord webhook | Free |
| Analysis | **You paste into Claude** | Free (your normal chat usage) |

No always-on server, no Anthropic API key, no per-scan cost. The only "limit" is
your regular Claude chat allowance — and you only spend it when an alert actually
fires and you choose to dig in.

## What trips an alert (all rules in `config.yaml`)

1. **Price move** — any watched ticker moves ≥ `move_threshold_pct` (default 2%).
2. **Catalyst** — a recent headline matches your theme keywords (Iran/oil, Fed/CPI…).
3. **Level cross** — a position crosses an `alert_above` / `alert_below` you set.

Catalyst beats level beats move when the same ticker trips more than one, so you
get one clean alert per ticker per scan.

---

## Setup (one time, ~10 min)

### 1. Create the repo
New GitHub repo (public is fine — no secrets in the code). Upload all files,
keeping the folder structure.

### 2. Get a Finnhub key
finnhub.io → free account → copy the API key.

### 3. Add Secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:
- `FINNHUB_KEY` (required for quotes/news)
- `DISCORD_WEBHOOK` (optional — for push alerts)

### 4. Discord push (optional, free)
In Discord: pick a channel → gear icon (Edit Channel) → Integrations → Webhooks →
New Webhook → name it → **Copy Webhook URL**. That single URL is your
`DISCORD_WEBHOOK` secret. No bot, no token, no chat ID.

Skip this and you still get the dashboard — you just check it instead of being pinged.

### 5. Turn on Pages
Settings → Pages → Deploy from branch → `main`, folder `/docs`. Dashboard lives at
`https://<you>.github.io/<repo>/`.

### 6. Allow Actions to commit
Settings → Actions → General → Workflow permissions → **Read and write** → Save.

### 7. Test
Actions tab → "Forkball Signals Scan" → **Run workflow**. Green = working. Market
closed? It logs "skipping" — correct. To force an off-hours test, set
`market_hours_only: false` in `config.yaml` temporarily.

---

## Daily use

1. Alert lands (Discord push or on the dashboard).
2. Tap **Copy paste-block for Claude** (dashboard) or copy the code block (Discord).
3. Paste into Claude in the app. Your market-analyst skill fires → full three-lens
   analysis, invalidation, the works.
4. You decide and execute manually.

## Tuning

- `move_threshold_pct` — the global default move that triggers an alert.
- `thresholds:` — per-ticker overrides. Tighten calm core holds (IVV at 1%), loosen
  volatile names (NVDA at 4%) so daily chop doesn't ping you. A position can also
  carry its own `move_threshold`, which wins over everything. Each alert shows the
  threshold that fired, so you can see why it triggered while tuning.
- `catalyst_themes` — add keywords/tickers for new patterns you're tracking.
- `alert_above` / `alert_below` on a position — level pings.
- Scan times live in `.github/workflows/scan.yml` (cron, UTC).

## Phase 2 (later)

Tighten the cron cadence (every 15–30 min) to approximate real-time catalyst
alerting, and sharpen the keyword filter against live noise. The analysis stays
where it is — in Claude, free, on demand.

## Disclaimer

Personal decision-support tool. Surfaces things for **you** to evaluate. Not advice
for anyone else — keep it personal and you're clear.
