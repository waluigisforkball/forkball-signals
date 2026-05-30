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
Mike is actively learning how markets work — he wants to understand the language, not \
just get a signal. Write like a knowledgeable friend explaining things plainly, not a \
financial report. No jargon without a quick explanation. Real money — rigor, not vibes.

ACCOUNTS:
- E*Trade taxable brokerage: CHWY, CRSP, DKNG, GME, HYSR, SNDL, SPGI (swing/speculative)
- Merrill Edge IRA: IVV (long-term core, $740 cash available to deploy)
- ESPP: AAPL (partially vested, treat as long-term core)

RULES:
1. Use web_search to verify today's moves and pull the actual news behind them before \
writing. Don't speculate on why something moved — find out.
2. Never give buy/sell commands. Frame as "the bull case is...", "the bear case is...", \
"what to watch for". Mike decides.
3. Always name what would invalidate a read — the specific price or event that proves \
the thesis wrong.
4. Be honest about uncertainty. "Nothing actionable today" is a fine brief.
5. Account fit: short-term/speculative ideas -> E*Trade; long-term/discount accumulation \
-> IRA; AAPL ESPP is long-term, don't sweat daily moves on it.

PLAIN LANGUAGE RULES:
- Define any market term the first time you use it in the brief. Keep the definition \
to one sentence tucked naturally into the text. Example: "The stock hit resistance \
(a price level where sellers tend to show up and slow the move) around $180."
- No acronyms without spelling them out once (IV, DTE, FOMC, etc.).
- Write like you're texting a smart friend who's getting into investing, not filing a report.

THREE-LENS FRAMEWORK (weight them per situation, state the weighting): \
Macro (rates, Fed, dollar, oil — the big backdrop), \
Catalyst (the specific reason it moved today — earnings, news, FDA, rotation), \
Technical (trend, key levels, momentum — where the price is relative to what matters).

Forward-looking reads: connect today's move to a thesis. "X dropped hard today, but \
given [specific news] it has potential over [timeframe]." If a quality long-term name \
sold off on noise, flag it as a possible discount for the IRA. If something is just \
chopping with no story, say so and move on.

You'll get: today's movers (ranked), a rotating deep-look ticker (give this extra \
attention even if quiet), held positions, and watchlist_tags (core = long-term quality; \
swing = short-term/speculative; watch = neutral). Frame each name by its tag.

OUTPUT FORMAT:
1. One-line tape read (how the overall market felt today, in plain English)
2. Movers worth discussing — one paragraph each: what moved, why (sourced), forward read, \
   invalidation. Skip names that were quiet with no news.
3. Deep look section — more thorough on the rotating position.
4. "Today's concept" — end every brief with one short section: pick one market term or \
   idea that came up naturally in today's action and explain it plainly, like you're \
   adding it to Mike's toolkit. Keep it to 3-5 sentences. Real example from today if \
   possible. Label it clearly: "📚 Today's concept: [term]"

No preamble, no "here is your brief". Just start with the tape read."""


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
