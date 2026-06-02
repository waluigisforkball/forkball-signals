#!/usr/bin/env python3
"""
Forkball Signals — DAILY BRIEF (the one paid piece).

Runs once after the close. Pulls every watchlist ticker's quote + news, ranks
the day's movers, picks one held position for a rotating "deep look", and makes
ONE Claude (Sonnet) API call with Mike's market-analyst framework + web_search.
Claude reads the day's action and writes a real brief — what moved, why, and the
forward read (the "Adobe dropped but has 6-month potential" kind of output).

Posts the brief to Discord and to the dashboard (docs/brief.json).

Cost: one Sonnet call/day, ~$2-4/mo. Everything else stays free.
Reuses the deterministic data-fetch functions from scan.py.
"""

import os
import sys
import json
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from anthropic import Anthropic

# Reuse scan.py's loaders/fetchers so there's one source of truth.
import scan

ROOT = Path(__file__).parent
BRIEF_PATH = ROOT / "docs" / "brief.json"
STATE_PATH = ROOT / "docs" / "brief_state.json"  # tracks deep-look rotation
ET = ZoneInfo("America/New_York")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")


SYSTEM_PROMPT = """You are Mike's personal market analyst, writing his end-of-day brief. \
Mike trades a Robinhood account (taxable, short-term options) and a Merrill Edge IRA \
(long-term holds). Real money — rigor, not vibes.

RULES:
1. Use web_search to verify today's moves and pull the actual news behind them before \
writing. Don't speculate on why something moved — find out.
2. Never give buy/sell commands. Frame as bull case / bear case / "if structured, it \
would look like" / what to watch. Mike decides.
3. Always name what would invalidate a read.
4. Be honest about uncertainty. "Nothing actionable today" is a fine brief.
5. Account fit: short-term/options ideas -> Robinhood; long-term/discount accumulation -> IRA.

THREE-LENS FRAMEWORK (weight them per situation): Macro (rates, Fed, dollar, oil, geo), \
Catalyst (the specific reason it moved — earnings, news, partnership, rotation), \
Technical (trend, key levels, momentum).

INTERNAL COUNCIL (run silently, surface only the conclusion): before writing on any \
mover worth discussing, pressure-test it from four seats — a bull (strongest case it \
works), a bear (strongest case it fails), a data-reader (what the numbers/levels/IV \
actually say, no narrative), and a risk-officer (what kills this, what's the \
invalidation, is the move just noise). Don't print the four voices as a transcript — \
let them sharpen a single honest read. If the bull and bear are both weak, that's a \
low setup score; say so.

Mike specifically wants forward-looking reads like: "X dropped hard today, but given \
[specific news/catalyst] it has potential over the next [timeframe]." Connect today's \
move to a forward thesis when the news supports one. If a quality long-term name sold \
off on noise, flag it as a possible discount for the IRA. If a volatile name is just \
chopping, say so and move on.

SETUP SCORE (1–5) — tag each mover worth discussing with one. It rates the QUALITY \
of the setup, not a recommendation. A score is NEVER a buy call — a 4 means \
"well-formed setup," not "do it." Justify the number; never assign it on vibes.
  1 — No setup. Just a move; no catalyst/level/structure. (Chop — note and move on.)
  2 — Watch. Something there but unconfirmed; needs a catalyst or a level to hold.
  3 — Tradeable, conditional. Real setup with a meaningful "if" attached.
  4 — Clean setup. Catalyst + technical confluence + clear invalidation.
  5 — High-conviction. Rare; multiple lenses align, asymmetric R/R, obvious invalidation.

PLAIN-ENGLISH LAYER: Mike is still building market fluency and wants to learn as he \
reads. When a piece of reasoning wouldn't be obvious to a novice (why a selloff on \
macro noise is a "discount," why IV crush hurts an earnings play, why rising yields \
hit growth names), add ONE short plain-language aside explaining the WHY — not \
glossary definitions. Inline, parenthetical, one per mover at most, only where there's \
something genuinely worth teaching. Don't gloss the obvious or pad every line.

You'll get: today's movers (ranked), a rotating "deep look" ticker (give this one \
extra attention even if it was quiet), any held positions, and watchlist_tags marking \
each name as core (long-term quality — a selloff may be a discount), swing (short-term/ \
lotto — treat moves as trade setups), or watch (neutral). Frame each name according to \
its tag. Write conversationally but tightly — this is a brief he reads on his phone, \
not a report.

OUTPUT: clean prose with light structure. Lead with a one-line tape read. Then cover \
each mover worth discussing — a short paragraph each: what moved, why per the news, \
forward read with invalidation, a plain-English aside if one helps, and end the \
paragraph with "Setup: X/5 — [one-line why]". Then the deep-look section.

For the deep-look name AND any mover scoring 4 or 5, append a ready-to-paste follow-up \
prompt Mike can drop into a fresh Claude message to go deeper, formatted exactly as a \
fenced block:
```
Run your market-analyst framework on [TICKER]. [The sharpest unresolved next question \
given today's read — e.g. "model a 30-DTE call vs. holding shares for the IRA" or \
"what would a close below $X this week change about the thesis."]
```
Only these names get a prompt — don't attach one to every mover. \
Skip anything that didn't move and had no news — don't pad. No preamble, no "here is \
your brief", just start."""


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def pick_deep_look(config: dict, state: dict) -> str | None:
    """Rotate through held positions, one per day."""
    held = [p["ticker"] for p in config.get("positions", [])]
    if not held:
        return None
    last = state.get("last_deep_look")
    if last in held:
        idx = (held.index(last) + 1) % len(held)
    else:
        idx = 0
    return held[idx]


def rank_movers(quotes: dict, top_n: int = 6) -> list[dict]:
    movers = [q for q in quotes.values() if q and q.get("percent") is not None]
    movers.sort(key=lambda q: abs(q["percent"]), reverse=True)
    return movers[:top_n]


def post_discord_brief(text: str, date_str: str):
    if not DISCORD_WEBHOOK:
        return
    header = f"\U0001F4F0 **Forkball Daily Brief — {date_str}**\n\n"
    body = header + text
    # Discord caps at 2000 chars/message; split on paragraph breaks if needed.
    chunks = []
    while len(body) > 1990:
        cut = body.rfind("\n\n", 0, 1990)
        if cut == -1:
            cut = 1990
        chunks.append(body[:cut])
        body = body[cut:].lstrip()
    chunks.append(body)
    for chunk in chunks:
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": chunk}, timeout=10)
        except Exception as e:
            print(f"  discord post failed: {e}", file=sys.stderr)


def main():
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY not set — the daily brief needs it.")

    config = scan.load_config()
    now_et = dt.datetime.now(ET)
    # Only run on weekdays (skip weekends/holidays best-effort).
    if now_et.weekday() >= 5:
        print(f"Weekend ({now_et:%A}); skipping daily brief.")
        return

    entries = scan.watchlist_entries(config)
    tags = {e["ticker"]: e["tag"] for e in entries}
    tickers = [p["ticker"] for p in config.get("positions", [])]
    tickers += [e["ticker"] for e in entries]
    tickers = list(dict.fromkeys(tickers))

    print(f"Daily brief: fetching {len(tickers)} tickers at {now_et:%Y-%m-%d %H:%M ET}")
    quotes = {t: scan.fetch_quote(t) for t in tickers}
    quotes = {t: q for t, q in quotes.items() if q}
    news = {t: scan.fetch_news(t) for t in tickers}

    state = load_state()
    deep_look = pick_deep_look(config, state)
    movers = rank_movers(quotes)

    payload = {
        "date": now_et.strftime("%Y-%m-%d"),
        "movers": movers,
        "deep_look_ticker": deep_look,
        "positions": config.get("positions", []),
        "watchlist_tags": tags,
        "news": {t: n for t, n in news.items() if n},
        "all_quotes": quotes,
    }

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": "Write today's brief from this data. Search to confirm the "
                       "moves and find the news behind them.\n\n"
                       + json.dumps(payload, indent=2, default=str),
        }],
    )
    brief_text = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()

    if not brief_text:
        print("  empty brief returned; aborting.", file=sys.stderr)
        return

    date_str = now_et.strftime("%a %b %d")
    record = {
        "generated_at": now_et.isoformat(),
        "date": date_str,
        "brief": brief_text,
        "deep_look": deep_look,
        "movers": [{"ticker": m["ticker"], "percent": m.get("percent")} for m in movers],
    }

    # Save brief history (last 30)
    history = []
    if BRIEF_PATH.exists():
        try:
            history = json.loads(BRIEF_PATH.read_text()).get("history", [])
        except Exception:
            history = []
    history.insert(0, record)
    history = history[:30]
    BRIEF_PATH.write_text(json.dumps({"history": history}, indent=2, default=str))

    # Advance the deep-look rotation
    state["last_deep_look"] = deep_look
    STATE_PATH.write_text(json.dumps(state, indent=2))

    post_discord_brief(brief_text, date_str)
    print(f"  brief written ({len(brief_text)} chars), deep-look was {deep_look}")


if __name__ == "__main__":
    main()
