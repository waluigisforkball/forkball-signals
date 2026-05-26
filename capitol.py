#!/usr/bin/env python3
"""
Forkball Signals — CAPITOL TRADES tracker (nightly, FREE).

Fetches US House + Senate stock-trade disclosures from the public Stock Watcher
JSON feeds (no API key). Detects newly-disclosed trades since the last run,
shows ALL of them on the dashboard, and pings Discord only for trades clearing a
dollar floor (or matching a spotlight name) so the channel stays signal-only.

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

import requests

import scan  # reuse load_config + fetch_quote

ROOT = Path(__file__).parent
OUT_PATH = ROOT / "docs" / "capitol.json"
SEEN_PATH = ROOT / "docs" / "capitol_seen.json"
ET = ZoneInfo("America/New_York")

# FMP "latest disclosures" feeds (new /stable/ format, free tier ~250 calls/day).
# These return the newest disclosures across ALL members, paginated — exactly what
# a tracker wants. Field names handled defensively in normalize() since FMP returns
# camelCase and occasionally varies. Needs a free key: FMP_API_KEY (GitHub secret).
FMP_BASE = "https://financialmodelingprep.com/stable"
FMP_KEY = os.environ.get("FMP_API_KEY", "")
SENATE_URL = f"{FMP_BASE}/senate-latest"
HOUSE_URL = f"{FMP_BASE}/house-latest"

# Separate webhook if you made a #capitol-trades channel; else falls back.
CAPITOL_WEBHOOK = os.environ.get("CAPITOL_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")


def parse_date(s: str):
    """Disclosure dates are MM/DD/YYYY. Return a date or None."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def fetch_feed(url: str, chamber: str, pages: int = 1) -> list[dict]:
    """Fetch FMP 'latest' disclosures. FMP's free tier caps `limit` at 25 AND
    only allows `page=0`, so we fetch a single page of 25 per chamber. That's
    the newest ~25 disclosures — plenty given the nightly cadence and the
    45-day filing lag. Deeper backfill would need a paid tier."""
    if not FMP_KEY:
        print(f"  FMP_API_KEY not set; cannot fetch {chamber}.", file=sys.stderr)
        return []
    PAGE_SIZE = 25  # free-tier max; FMP 402s on anything higher.
    out = []
    for page in range(pages):
        try:
            r = requests.get(url, timeout=30, params={
                "page": page, "limit": PAGE_SIZE, "apikey": FMP_KEY,
            }, headers={"User-Agent": "forkball-signals"})
            # Surface non-200s explicitly — a 401/402/403 here means the key is
            # bad or the endpoint moved to a paid tier, which is the most likely
            # reason this feed silently returns nothing.
            if r.status_code != 200:
                print(f"  {chamber} HTTP {r.status_code}: {r.text[:200]}",
                      file=sys.stderr)
                break
            data = r.json()
            # FMP signals errors as a dict with various keys depending on the
            # failure; log whatever it sent rather than guessing the key.
            if isinstance(data, dict):
                print(f"  {chamber} FMP returned an error object: "
                      f"{json.dumps(data)[:200]}", file=sys.stderr)
                break
            if not isinstance(data, list) or not data:
                break
            for it in data:
                it["_chamber"] = chamber
            out += data
            if len(data) < PAGE_SIZE:
                break  # last page
        except Exception as e:
            print(f"  {chamber} feed fetch failed (page {page}): {e}", file=sys.stderr)
            break
    return out


def g(d: dict, *keys, default=""):
    """Grab the first present key — field names differ across the two feeds."""
    for k in keys:
        if d.get(k) not in (None, "", "--"):
            return d[k]
    return default


def parse_amount_floor(amount_str: str) -> int:
    """Disclosures give a range like '$1,001 - $15,000'. Return the LOW end as int.
    Strip commas first so each dollar figure stays a whole number, then take the
    smallest figure found (the bottom of the range)."""
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
    # FMP returns camelCase; names may be split into first/last or combined.
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


def load_seen() -> set:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text()).get("ids", []))
        except Exception:
            return set()
    return set()


def post_discord(trades: list[dict]):
    if not CAPITOL_WEBHOOK or not trades:
        return
    # Batch into messages under Discord's 2000-char cap.
    def line(t):
        emoji = "\U0001F7E2" if "BUY" in t["type"] or "PURCHASE" in t["type"] else \
                ("\U0001F534" if "S" in t["type"] else "\u26AA")
        tk = f"${t['ticker']}" if t["ticker"] else (t["asset"][:30] or "—")
        px = ""
        if t.get("price"):
            px = f" @ ${t['price']:.2f}"
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


def main():
    config = scan.load_config()
    cap = config.get("capitol", {})
    now_et = dt.datetime.now(ET)

    raw = []
    if cap.get("track_house", True):
        raw += fetch_feed(HOUSE_URL, "House")
    if cap.get("track_senate", True):
        raw += fetch_feed(SENATE_URL, "Senate")
    print(f"Fetched {len(raw)} raw disclosures")

    if not raw:
        print("  no data (feed down, key bad, or endpoint now paid). "
              "Preserving prior dashboard; ensuring state files exist.")
        # Make sure both committed files exist so the workflow's `git add`
        # never fails on a missing pathspec. Preserve whatever was there.
        if not OUT_PATH.exists():
            OUT_PATH.write_text(json.dumps({
                "generated_at": now_et.isoformat(),
                "alert_floor": cap.get("alert_min_amount", 50000),
                "trades": [],
            }, indent=2))
        if not SEEN_PATH.exists():
            SEEN_PATH.write_text(json.dumps({"ids": []}, indent=2))
        return

    # Normalize + keep only recently FILED disclosures (FMP gives a real filing
    # date). Fall back to transaction date if filing date is absent.
    lookback = cap.get("lookback_days", 14)
    cutoff = now_et.date() - dt.timedelta(days=lookback)
    trades = []
    for r in raw:
        t = normalize(r)
        if not t["politician"]:
            continue
        d = parse_date(t["filing_date"]) or parse_date(t["trade_date"])
        if d and d < cutoff:
            continue
        trades.append(t)

    # New = not in the seen-set. On the very FIRST run the seen-set is empty, so
    # everything would look "new" — guard against alert-flooding by treating the
    # first run as seed-only (populate seen, don't alert).
    seen = load_seen()
    first_run = not SEEN_PATH.exists()
    new_trades = [] if first_run else [t for t in trades if trade_id(t) not in seen]
    print(f"  {len(trades)} recent, {len(new_trades)} new"
          + (" (FIRST RUN: seeding, no alerts)" if first_run else ""))

    # Decide which NEW trades clear the alert bar (dollar floor or spotlight name).
    floor = cap.get("alert_min_amount", 50000)
    spotlight = [s.lower() for s in cap.get("spotlight_politicians", [])]

    def alertworthy(t):
        if any(s in t["politician"].lower() for s in spotlight):
            return True
        return t["amount_floor"] >= floor

    to_alert = [t for t in new_trades if alertworthy(t)]
    print(f"  {len(to_alert)} clear the alert bar (floor ${floor:,} or spotlight)")

    # Enrich alert-worthy trades with a current quote (cheap, only these).
    for t in to_alert:
        if t["ticker"]:
            q = scan.fetch_quote(t["ticker"])
            t["price"] = q["price"] if q else None

    post_discord(to_alert)

    # Dashboard shows ALL recent trades (the full firehose), newest first.
    def sort_key(t):
        return parse_date(t["trade_date"]) or dt.date.min
    trades.sort(key=sort_key, reverse=True)

    OUT_PATH.write_text(json.dumps({
        "generated_at": now_et.isoformat(),
        "alert_floor": floor,
        "trades": trades[:300],
    }, indent=2, default=str))

    # Update seen set (cap growth).
    all_ids = list(seen | {trade_id(t) for t in trades})
    SEEN_PATH.write_text(json.dumps({"ids": all_ids[-5000:]}, indent=2))
    print(f"  wrote dashboard ({len(trades)} trades) + seen state")


if __name__ == "__main__":
    main()
