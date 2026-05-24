#!/usr/bin/env python3
"""
Forkball Signals — scheduled, ZERO-COST market tripwire.

No AI, no API tokens. This is deterministic Python: it fetches quotes + news,
checks your rules (price moves, catalyst keywords, level crosses), and when
something trips it (a) writes an alert to the dashboard and (b) pushes you a
Telegram message containing a ready-to-paste block for Claude.

You tap, copy the block, paste into Claude in the app -> your mike-market-analyst
skill fires for free. The scanner finds; you analyze.

Cost: $0. GitHub Actions + Finnhub free tier + Telegram + GitHub Pages.
"""

import os
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path

import yaml
import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
OUTPUT_PATH = ROOT / "docs" / "recommendations.json"
ET = ZoneInfo("America/New_York")

FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def market_is_open(now_et: dt.datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now_et <= close_t


# ---------------------------------------------------------------------------
# Data layer (deterministic)
# ---------------------------------------------------------------------------
def fetch_quote(ticker: str) -> dict | None:
    if not FINNHUB_KEY:
        return None
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": FINNHUB_KEY},
            timeout=10,
        )
        d = r.json()
        if not d or d.get("c") in (0, None):
            return None
        return {
            "ticker": ticker,
            "price": d["c"],
            "change": d.get("d"),
            "percent": d.get("dp"),
            "high": d.get("h"),
            "low": d.get("l"),
            "prev_close": d.get("pc"),
        }
    except Exception as e:
        print(f"  quote fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


def fetch_news(ticker: str, days: int = 3) -> list[dict]:
    if not FINNHUB_KEY:
        return []
    try:
        today = dt.date.today()
        frm = today - dt.timedelta(days=days)
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": frm.isoformat(),
                    "to": today.isoformat(), "token": FINNHUB_KEY},
            timeout=10,
        )
        items = r.json() or []
        return [
            {"headline": it.get("headline"), "source": it.get("source"),
             "url": it.get("url")}
            for it in items[:5]
        ]
    except Exception as e:
        print(f"  news fetch failed for {ticker}: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Rule engine — this is where alerts are decided (no AI)
# ---------------------------------------------------------------------------
def check_rules(config: dict, quotes: dict, news: dict) -> list[dict]:
    """Return a list of triggered alerts."""
    alerts = []
    settings = config.get("settings", {})
    default_threshold = settings.get("move_threshold_pct", 2.0)
    # Optional per-ticker overrides in config -> settings.thresholds: {TICKER: pct}
    overrides = {k.upper(): v for k, v in (settings.get("thresholds") or {}).items()}

    held = {p["ticker"]: p for p in config.get("positions", [])}

    def threshold_for(ticker: str) -> float:
        # Priority: per-position move_threshold > settings.thresholds > global default
        pos = held.get(ticker)
        if pos and pos.get("move_threshold") is not None:
            return pos["move_threshold"]
        if ticker.upper() in overrides:
            return overrides[ticker.upper()]
        return default_threshold

    # Rule 1: significant price move on any watched ticker
    for ticker, q in quotes.items():
        if not q or q.get("percent") is None:
            continue
        thr = threshold_for(ticker)
        if abs(q["percent"]) >= thr:
            alerts.append({
                "type": "move",
                "ticker": ticker,
                "reason": f"{ticker} moved {q['percent']:+.2f}% "
                          f"(current ${q['price']:.2f}, threshold ±{thr}%)",
                "held": ticker in held,
                "position": held.get(ticker),
                "quote": q,
                "headlines": news.get(ticker, []),
            })

    # Rule 2: catalyst keyword hit in any fetched headline
    for theme in config.get("catalyst_themes", []):
        kws = [k.lower() for k in theme.get("keywords", [])]
        for ticker, items in news.items():
            for item in items:
                hl = (item.get("headline") or "").lower()
                matched = [k for k in kws if k in hl]
                if matched:
                    alerts.append({
                        "type": "catalyst",
                        "ticker": ticker,
                        "reason": f"Catalyst theme '{theme.get('note','')}' — "
                                  f"matched: {', '.join(matched)}",
                        "theme": theme.get("note", ""),
                        "relevant_tickers": theme.get("relevant_tickers", []),
                        "held": ticker in held,
                        "quote": quotes.get(ticker),
                        "headlines": [item],
                    })

    # Rule 3: position level crosses (if you set alert_above / alert_below)
    for p in config.get("positions", []):
        q = quotes.get(p["ticker"])
        if not q:
            continue
        above = p.get("alert_above")
        below = p.get("alert_below")
        if above and q["price"] >= above:
            alerts.append({
                "type": "level", "ticker": p["ticker"],
                "reason": f"{p['ticker']} crossed ABOVE ${above} (now ${q['price']:.2f})",
                "held": True, "position": p, "quote": q,
                "headlines": news.get(p["ticker"], []),
            })
        if below and q["price"] <= below:
            alerts.append({
                "type": "level", "ticker": p["ticker"],
                "reason": f"{p['ticker']} dropped BELOW ${below} (now ${q['price']:.2f})",
                "held": True, "position": p, "quote": q,
                "headlines": news.get(p["ticker"], []),
            })

    # De-dupe: one alert per ticker, prefer catalyst > level > move
    priority = {"catalyst": 3, "level": 2, "move": 1}
    best = {}
    for a in alerts:
        t = a["ticker"]
        if t not in best or priority[a["type"]] > priority[best[t]["type"]]:
            best[t] = a
    return list(best.values())


# ---------------------------------------------------------------------------
# Build the paste-into-Claude block
# ---------------------------------------------------------------------------
def build_paste_block(alert: dict) -> str:
    q = alert.get("quote") or {}
    lines = [f"Run your market-analyst framework on this.", ""]
    lines.append(f"Ticker: {alert['ticker']}")
    if q:
        lines.append(
            f"Price: ${q.get('price','?')} "
            f"({q.get('percent','?'):+.2f}% today, "
            f"day range ${q.get('low','?')}–${q.get('high','?')})"
            if q.get("percent") is not None else f"Price: ${q.get('price','?')}"
        )
    if alert.get("held") and alert.get("position"):
        p = alert["position"]
        pos = f"I HOLD this: {p.get('type','')} in {p.get('account','')}"
        if p.get("strike"):
            pos += f", strike ${p['strike']} exp {p.get('expiry','')}"
        lines.append(pos)
    lines.append(f"Trigger: {alert['reason']}")
    if alert.get("relevant_tickers"):
        lines.append(f"Related tickers: {', '.join(alert['relevant_tickers'])}")
    heads = alert.get("headlines", [])
    if heads:
        lines.append("Recent headlines:")
        for h in heads[:4]:
            lines.append(f"  - {h.get('headline','')} ({h.get('source','')}) {h.get('url','')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def push_telegram(alert: dict, paste: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    icon = {"move": "\U0001F4C8", "catalyst": "\u26A1", "level": "\U0001F3AF"}.get(alert["type"], "\U0001F514")
    held = " (you hold)" if alert.get("held") else ""
    # The paste block goes in a code block so you can one-tap copy it in Telegram.
    msg = (
        f"{icon} *{alert['ticker']}*{held}\n"
        f"{alert['reason']}\n\n"
        f"Paste into Claude:\n"
        f"```\n{paste}\n```"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"  telegram push failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
def main():
    config = load_config()
    settings = config.get("settings", {})
    now_et = dt.datetime.now(ET)

    if settings.get("market_hours_only", True) and not market_is_open(now_et):
        print(f"Market closed at {now_et:%Y-%m-%d %H:%M ET}; skipping.")
        return

    tickers = [p["ticker"] for p in config.get("positions", [])]
    tickers += config.get("watchlist", [])
    tickers = list(dict.fromkeys(tickers))
    print(f"Scanning {len(tickers)} tickers at {now_et:%Y-%m-%d %H:%M ET}")

    quotes = {t: fetch_quote(t) for t in tickers}
    quotes = {t: q for t, q in quotes.items() if q}
    news = {t: fetch_news(t) for t in tickers}

    alerts = check_rules(config, quotes, news)
    print(f"  {len(alerts)} alert(s) triggered")

    # Attach paste blocks
    for a in alerts:
        a["paste_block"] = build_paste_block(a)

    record = {
        "generated_at": now_et.isoformat(),
        "alerts": alerts,
        "quotes": quotes,
        "scanned": tickers,
    }

    history = []
    if OUTPUT_PATH.exists():
        try:
            history = json.loads(OUTPUT_PATH.read_text()).get("history", [])
        except Exception:
            history = []
    history.insert(0, record)
    history = history[:50]
    OUTPUT_PATH.write_text(json.dumps({"history": history}, indent=2, default=str))
    print(f"  wrote {OUTPUT_PATH}")

    for a in alerts:
        push_telegram(a, a["paste_block"])
        print(f"  pushed {a['ticker']} ({a['type']})")


if __name__ == "__main__":
    main()
