#!/usr/bin/env python3
"""
Forkball Signals — CAPITOL TRADES tracker (nightly, FREE).

Fetches US House + Senate stock-trade disclosures from the public Stock Watcher
JSON feeds (no API key). Detects newly-disclosed trades since the last run,
shows ALL of them on the dashboard, and pings Discord only for trades clearing a
dollar floor (or matching a spotlight name) so the channel stays signal-only.

Pattern detection: tracks per-politician trade history in capitol_seen.json and
fires a separate Discord ping the first time a politician makes repeated same-
direction bets on the same ticker within a rolling window. Pattern cards also
appear at the top of the Capitol tab on the dashboard.

Data note: STOCK Act filings can lag up to 45 days after the actual trade. This
tracks what was *newly disclosed*, not real-time activity. No tracker can be
real-time — that's the law, not a limitation here.

Cost: $0. Public JSON + GitHub Actions + Discord. Optional Finnhub quote enrich.
"""

import os
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict

import requests

import scan  # reuse load_config + fetch_quote

ROOT = Path(__file__).parent
OUT_PATH = ROOT / "docs" / "capitol.json"
SEEN_PATH = ROOT / "docs" / "capitol_seen.json"
ET = ZoneInfo("America/New_York")

FMP_BASE = "https://financialmodelingprep.com/stable"
FMP_KEY = os.environ.get("FMP_API_KEY", "")
SENATE_URL = f"{FMP_BASE}/senate-latest"
HOUSE_URL = f"{FMP_BASE}/house-latest"

CAPITOL_WEBHOOK = os.environ.get("CAPITOL_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

# Pattern detection settings
PATTERN_WINDOW_DAYS = 60   # rolling window for repeat-bet detection
PATTERN_MIN_TRADES = 2     # how many same-direction trades = a pattern


def parse_date(s: str):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def fetch_feed(url: str, chamber: str, pages: int = 3) -> list[dict]:
    if not FMP_KEY:
        print(f"  FMP_API_KEY not set; cannot fetch {chamber}.", file=sys.stderr)
        return []
    out = []
    for page in range(pages):
        try:
            r = requests.get(url, timeout=30, params={
                "page": page, "limit": 100, "apikey": FMP_KEY,
            }, headers={"User-Agent": "forkball-signals"})
            data = r.json()
            if isinstance(data, dict) and data.get("Error Message"):
                print(f"  {chamber} FMP error: {data['Error Message']}", file=sys.stderr)
                break
            if not isinstance(data, list) or not data:
                break
            for it in data:
                it["_chamber"] = chamber
            out += data
            if len(data) < 100:
                break
        except Exception as e:
            print(f"  {chamber} feed fetch failed (page {page}): {e}", file=sys.stderr)
            break
    return out


def g(d: dict, *keys, default=""):
    for k in keys:
        if d.get(k) not in (None, "", "--"):
            return d[k]
    return default


def parse_amount_floor(amount_str: str) -> int:
    if not amount_str:
        return 0
    cleaned = str(amount_str).replace(",", "")
    nums = []
    cur = ""
    for ch in cleaned:
        if ch.isdigit():
            cur += ch
        elif cur:
            nums.append(int(cur)); cur = ""
    if cur:
        nums.append(int(cur))
    return min(nums) if nums else 0


def normalize(raw: dict) -> dict:
    name = g(raw, "office", "representative", "senator", "name", "Name")
    if not name:
        first = g(raw, "firstName", "first_name")
        last = g(raw, "lastName", "last_name")
        name = f"{first} {last}".strip()
    ticker = g(raw, "symbol", "ticker", "Ticker", default="").upper()
    amount = g(raw, "amount", "Amount")
    tdate = parse_date(g(raw, "transactionDate", "transaction_date", "trade_date"))
    fdate = parse_date(g(raw, "disclosureDate", "disclosure_date", "filing_date"))
    return {
        "politician": name,
        "chamber": raw.get("_chamber", ""),
        "ticker": ticker if ticker and ticker != "--" else "",
        "type": g(raw, "type", "Type", "transaction_type").upper(),
        "amount": amount,
        "amount_floor": parse_amount_floor(amount),
        "trade_date": tdate.isoformat() if tdate else "",
        "filing_date": fdate.isoformat() if fdate else "",
        "asset": g(raw, "assetDescription", "asset_description", default=""),
        "link": g(raw, "link", "ptr_link", "url", default=""),
    }


def trade_id(t: dict) -> str:
    return f"{t['politician']}:{t['ticker']}:{t['trade_date']}:{t['type']}:{t['amount_floor']}"


def is_buy(t: dict) -> bool:
    return bool(t.get("ticker")) and ("BUY" in t["type"] or "PURCHASE" in t["type"])

def is_sell(t: dict) -> bool:
    return bool(t.get("ticker")) and ("SELL" in t["type"] or "SALE" in t["type"])

def trade_direction(t: dict) -> str | None:
    if is_buy(t): return "BUY"
    if is_sell(t): return "SELL"
    return None


def load_seen() -> dict:
    """Returns full seen state: {ids: [...], patterns_alerted: [...], trade_history: {...}}"""
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text())
        except Exception:
            return {}
    return {}


def detect_patterns(all_trades: list[dict], now: dt.date) -> list[dict]:
    """
    Scan all_trades for politicians who have made 2+ same-direction trades
    on the same ticker within PATTERN_WINDOW_DAYS. Returns a list of pattern dicts.
    Only includes trades with a real ticker and a parseable trade_date.
    """
    cutoff = now - dt.timedelta(days=PATTERN_WINDOW_DAYS)

    # Group by (politician, ticker, direction)
    groups = defaultdict(list)
    for t in all_trades:
        direction = trade_direction(t)
        if not direction or not t.get("ticker"):
            continue
        d = parse_date(t["trade_date"])
        if not d or d < cutoff:
            continue
        key = (t["politician"], t["ticker"], direction)
        groups[key].append(t)

    patterns = []
    for (pol, ticker, direction), trades in groups.items():
        if len(trades) < PATTERN_MIN_TRADES:
            continue
        trades_sorted = sorted(trades, key=lambda x: parse_date(x["trade_date"]) or dt.date.min)
        first_date = trades_sorted[0]["trade_date"]
        last_date = trades_sorted[-1]["trade_date"]
        span_days = (
            (parse_date(last_date) - parse_date(first_date)).days
            if parse_date(last_date) and parse_date(first_date) else 0
        )
        total_floor = sum(t["amount_floor"] for t in trades)
        chamber = trades_sorted[-1].get("chamber", "")
        patterns.append({
            "politician": pol,
            "chamber": chamber,
            "ticker": ticker,
            "direction": direction,
            "trade_count": len(trades),
            "first_trade": first_date,
            "last_trade": last_date,
            "span_days": span_days,
            "total_amount_floor": total_floor,
            "amounts": [t["amount"] for t in trades_sorted],
        })

    # Sort: most trades first, then by recency of last trade
    patterns.sort(key=lambda p: (-p["trade_count"], p["last_trade"]), reverse=False)
    patterns.sort(key=lambda p: p["trade_count"], reverse=True)
    return patterns


def pattern_id(p: dict) -> str:
    return f"pattern:{p['politician']}:{p['ticker']}:{p['direction']}:{p['trade_count']}"


def post_discord_trades(trades: list[dict]):
    if not CAPITOL_WEBHOOK or not trades:
        return
    def line(t):
        emoji = "\U0001F7E2" if is_buy(t) else ("\U0001F534" if is_sell(t) else "\u26AA")
        tk = f"${t['ticker']}" if t["ticker"] else (t["asset"][:30] or "—")
        px = f" @ ${t['price']:.2f}" if t.get("price") else ""
        return (f"{emoji} **{t['politician']}** ({t['chamber']}) — {t['type']} "
                f"{tk}{px}\n   {t['amount']} · traded {t['trade_date']} · filed {t['filing_date']}")
    header = "\U0001F3DB\uFE0F **Newly disclosed Capitol trades**\n\n"
    buf = header
    for t in trades:
        ln = line(t) + "\n"
        if len(buf) + len(ln) > 1950:
            try:
                requests.post(CAPITOL_WEBHOOK, json={"content": buf}, timeout=10)
            except Exception as e:
                print(f"  discord post failed: {e}", file=sys.stderr)
            buf = ""
        buf += ln
    if buf.strip():
        try:
            requests.post(CAPITOL_WEBHOOK, json={"content": buf}, timeout=10)
        except Exception as e:
            print(f"  discord post failed: {e}", file=sys.stderr)


def post_discord_patterns(new_patterns: list[dict]):
    """Fire one Discord message per new pattern detected."""
    if not CAPITOL_WEBHOOK or not new_patterns:
        return
    for p in new_patterns:
        emoji = "\U0001F501"  # 🔁
        dir_emoji = "\U0001F7E2" if p["direction"] == "BUY" else "\U0001F534"
        amounts_str = " → ".join(p["amounts"])
        content = (
            f"{emoji} **Pattern Alert** — {p['politician']} ({p['chamber']})\n"
            f"{dir_emoji} **{p['trade_count']}x {p['direction']}** on **${p['ticker']}** "
            f"over {p['span_days']} days\n"
            f"Amounts: {amounts_str}\n"
            f"First: {p['first_trade']} · Last: {p['last_trade']}\n"
            f"_(45-day filing lag applies — trades may have occurred earlier)_"
        )
        try:
            requests.post(CAPITOL_WEBHOOK, json={"content": content}, timeout=10)
            print(f"  pattern alert sent: {p['politician']} {p['direction']} {p['ticker']} x{p['trade_count']}")
        except Exception as e:
            print(f"  pattern discord post failed: {e}", file=sys.stderr)


def main():
    config = scan.load_config()
    cap = config.get("capitol", {})
    now_et = dt.datetime.now(ET)
    today = now_et.date()

    raw = []
    if cap.get("track_house", True):
        raw += fetch_feed(HOUSE_URL, "House")
    if cap.get("track_senate", True):
        raw += fetch_feed(SENATE_URL, "Senate")
    print(f"Fetched {len(raw)} raw disclosures")

    if not raw:
        print("  no data (feed down or FMP_API_KEY missing); leaving state untouched.")
        return

    lookback = cap.get("lookback_days", 14)
    cutoff = today - dt.timedelta(days=lookback)
    trades = []
    for r in raw:
        t = normalize(r)
        if not t["politician"]:
            continue
        d = parse_date(t["filing_date"]) or parse_date(t["trade_date"])
        if d and d < cutoff:
            continue
        trades.append(t)

    seen_state = load_seen()
    seen_ids = set(seen_state.get("ids", []))
    patterns_alerted = set(seen_state.get("patterns_alerted", []))
    first_run = not SEEN_PATH.exists()

    new_trades = [] if first_run else [t for t in trades if trade_id(t) not in seen_ids]
    print(f"  {len(trades)} recent, {len(new_trades)} new"
          + (" (FIRST RUN: seeding, no alerts)" if first_run else ""))

    floor = cap.get("alert_min_amount", 50000)
    spotlight = [s.lower() for s in cap.get("spotlight_politicians", [])]

    def alertworthy(t):
        if any(s in t["politician"].lower() for s in spotlight):
            return True
        return t["amount_floor"] >= floor

    to_alert = [t for t in new_trades if alertworthy(t)]
    print(f"  {len(to_alert)} clear the alert bar (floor ${floor:,} or spotlight)")

    for t in to_alert:
        if t["ticker"]:
            q = scan.fetch_quote(t["ticker"])
            t["price"] = q["price"] if q else None

    post_discord_trades(to_alert)

    # --- Pattern detection ---
    # Use a broader window of trades for pattern analysis: combine current batch
    # with any previously stored trades from capitol.json for richer history.
    all_trades_for_patterns = list(trades)
    if OUT_PATH.exists():
        try:
            old_data = json.loads(OUT_PATH.read_text())
            old_trades = old_data.get("trades", [])
            # Merge, dedup by trade_id
            existing_ids = {trade_id(t) for t in all_trades_for_patterns}
            for ot in old_trades:
                if trade_id(ot) not in existing_ids:
                    all_trades_for_patterns.append(ot)
        except Exception:
            pass

    patterns = detect_patterns(all_trades_for_patterns, today)
    print(f"  {len(patterns)} pattern(s) detected across {PATTERN_WINDOW_DAYS}-day window")

    # Only alert on patterns we haven't pinged before, and skip on first run
    new_patterns = [] if first_run else [
        p for p in patterns if pattern_id(p) not in patterns_alerted
    ]
    if new_patterns:
        print(f"  {len(new_patterns)} new pattern(s) to alert")
        post_discord_patterns(new_patterns)

    # Dashboard: sort trades newest first
    def sort_key(t):
        return parse_date(t["trade_date"]) or dt.date.min
    trades.sort(key=sort_key, reverse=True)

    OUT_PATH.write_text(json.dumps({
        "generated_at": now_et.isoformat(),
        "alert_floor": floor,
        "patterns": patterns,
        "trades": trades[:300],
    }, indent=2, default=str))

    # Update seen state
    all_ids = list(seen_ids | {trade_id(t) for t in trades})
    all_patterns_alerted = list(patterns_alerted | {pattern_id(p) for p in new_patterns})
    SEEN_PATH.write_text(json.dumps({
        "ids": all_ids[-5000:],
        "patterns_alerted": all_patterns_alerted[-500:],
    }, indent=2))
    print(f"  wrote dashboard ({len(trades)} trades, {len(patterns)} patterns) + seen state")


if __name__ == "__main__":
    main()
