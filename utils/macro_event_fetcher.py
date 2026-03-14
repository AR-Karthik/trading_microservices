"""
utils/macro_event_fetcher.py
============================
Macro Event Data Pipeline (SRS §2.3 / §10)

Fetches upcoming macro events from two sources:
  1. Forex Factory JSON API  — global macroeconomic events (USD, EUR, GBP, INR)
  2. Financial Modeling Prep (FMP)  — earnings & economic calendar filtered on US + India

Merges both feeds, deduplicates, filters to tier-1 events (impact=high),
and writes to:
  - data/macro_calendar.json  (used by system_controller.py at startup)
  - Redis key "macro_events"  (updated in-process for running system_controller)

Called from:
  - infrastructure/gcp_provision.py  before VM startup (Cloud Function context)
  - Could also be scheduled as a standalone cron at 07:00 IST daily

Dependencies: requests (already in requirements.txt), python-holidays (for NSE check)

FMP requires API key in env: FMP_API_KEY
ForexFactory is public (JSON endpoint, no auth needed).
"""

import json
import logging
import os
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import asyncio
import socket
from core.network_utils import exponential_backoff

# --- Simple DNS Caching (SRS §127) ---
_dns_cache = {}
_real_getaddrinfo = socket.getaddrinfo

def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    key = (host, port, family, type, proto, flags)
    if key in _dns_cache:
        return _dns_cache[key]
    res = _real_getaddrinfo(host, port, family, type, proto, flags)
    _dns_cache[key] = res
    return res

socket.getaddrinfo = _cached_getaddrinfo

logger = logging.getLogger("MacroEventFetcher")

IST = ZoneInfo("Asia/Kolkata")

# ── Forex Factory ─────────────────────────────────────────────────────────────
# ForexFactory exposes a weekly JSON calendar: https://nfs.faireconomy.media/ff_calendar_thisweek.json
# We also fetch next week's for look-ahead.
FF_CURRENT_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_WEEK_URL    = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

# Currencies relevant to NSE/BankNifty trading
RELEVANT_CURRENCIES = {"USD", "INR", "EUR", "GBP", "JPY"}

# ── Financial Modeling Prep ───────────────────────────────────────────────────
FMP_BASE = "https://financialmodelingprep.com/api/v3"
FMP_API_KEY = os.getenv("FMP_API_KEY", "")

# Countries we care about: US + India
FMP_COUNTRIES = {"US", "IN"}

# Output path (relative to project root)
CALENDAR_PATH = Path(__file__).parent.parent / "data" / "macro_calendar.json"

# HTTP request timeout
REQUEST_TIMEOUT = 15


@exponential_backoff(max_retries=3)
async def fetch_forex_factory() -> list[dict]:
    """
    Fetches ForexFactory JSON calendar for current + next week.
    Returns list of normalised event dicts.
    """
    events = []
    for url in [FF_CURRENT_WEEK_URL, FF_NEXT_WEEK_URL]:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "TradingBot/1.0"})
            if resp.status_code != 200:
                logger.warning(f"ForexFactory returned {resp.status_code} for {url}")
                continue
            raw = resp.json()
            for event in raw:
                currency = event.get("currency", "").upper()
                impact = event.get("impact", "").lower()

                # Only high-impact events in relevant currencies
                if currency not in RELEVANT_CURRENCIES or impact != "high":
                    continue

                # Parse event datetime (FF uses UTC ISO format)
                dt_str = event.get("date", "")
                if not dt_str:
                    continue
                try:
                    event_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                events.append({
                    "name": event.get("title", "Unknown"),
                    "datetime": event_dt.astimezone(IST).isoformat(),
                    "tier": 1,  # All high-impact → tier-1
                    "currency": currency,
                    "impact": "HIGH",
                    "source": "forex_factory",
                    "country": event.get("country", ""),
                })
            logger.info(f"ForexFactory: {len(events)} high-impact events from {url}")
        except Exception as e:
            logger.error(f"ForexFactory fetch error ({url}): {e}")

    return events


@exponential_backoff(max_retries=3)
async def fetch_fmp_economic_calendar(days_ahead: int = 14) -> list[dict]:
    """
    Fetches economic calendar events from FMP for next `days_ahead` days.
    Filters on US (USD) and India (INR) with high impact.
    Returns normalised event dicts.
    """
    if not FMP_API_KEY:
        logger.warning("FMP_API_KEY not set — skipping FMP fetch.")
        return []

    today = date.today()
    end = today + timedelta(days=days_ahead)
    url = (
        f"{FMP_BASE}/economic_calendar"
        f"?from={today.isoformat()}&to={end.isoformat()}"
        f"&apikey={FMP_API_KEY}"
    )
    events = []
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"FMP returned {resp.status_code}: {resp.text[:200]}")
            return []

        raw = resp.json()
        for event in raw:
            country = (event.get("country") or "").upper()
            impact = (event.get("impact") or "").lower()

            if country not in FMP_COUNTRIES:
                continue
            if impact != "high":
                continue

            dt_str = event.get("date", "")
            if not dt_str:
                continue
            try:
                # FMP format: "2025-11-05 14:30:00"
                event_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    event_dt = datetime.fromisoformat(dt_str)
                except ValueError:
                    continue

            events.append({
                "name": event.get("event", "Unknown"),
                "datetime": event_dt.astimezone(IST).isoformat(),
                "tier": 1,
                "currency": event.get("currency", ""),
                "impact": "HIGH",
                "source": "fmp",
                "country": country,
                "actual": event.get("actual"),
                "estimate": event.get("estimate"),
                "previous": event.get("previous"),
            })

        logger.info(f"FMP: fetched {len(events)} high-impact US/India events.")
    except Exception as e:
        logger.error(f"FMP fetch error: {e}")

    return events


def deduplicate(events: list[dict]) -> list[dict]:
    """
    Deduplicates events by (name_lower + date).
    FMP and ForexFactory may overlap on major US data releases.
    """
    seen = set()
    unique = []
    for ev in events:
        # Use first 30 chars of name + date (YYYY-MM-DD) as dedup key
        key = (ev["name"][:30].lower().strip(), ev["datetime"][:10])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique


def sort_chronologically(events: list[dict]) -> list[dict]:
    """Sort events by datetime ascending."""
    return sorted(events, key=lambda e: e["datetime"])


async def fetch_and_write(write_to_disk: bool = True, write_to_redis: bool = False,
                    redis_client=None) -> list[dict]:
    """
    Main entrypoint: fetches from both sources, merges, deduplicates, sorts.
    Writes to macro_calendar.json and optionally to Redis.

    Returns the final event list.
    """
    logger.info("Starting macro event fetch (ForexFactory + FMP)...")

    all_events = []
    # Using asyncio.gather instead of sequential calls
    results = await asyncio.gather(
        fetch_forex_factory(),
        fetch_fmp_economic_calendar(),
        return_exceptions=True
    )
    for res in results:
        if isinstance(res, list):
            all_events.extend(res)
        else:
            logger.error(f"Fetch task failed: {res}")

    merged = deduplicate(all_events)
    merged = sort_chronologically(merged)

    logger.info(f"Total unique tier-1 macro events: {len(merged)}")

    if write_to_disk:
        CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CALENDAR_PATH, "w") as f:
            json.dump(merged, f, indent=2, default=str)
        logger.info(f"Macro calendar written to {CALENDAR_PATH}")

    if write_to_redis and redis_client is not None:
        try:
            redis_client.set("macro_events", json.dumps(merged))
            logger.info("Macro events pushed to Redis key 'macro_events'")
        except Exception as e:
            logger.error(f"Redis write error: {e}")

    return merged


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        stream=sys.stdout)
    events = asyncio.run(fetch_and_write(write_to_disk=True, write_to_redis=False))
    print(f"\nFetched {len(events)} events. Saved to {CALENDAR_PATH}")
    for ev in events[:10]:
        print(f"  [{ev['source']:15}] {ev['datetime'][:16]} {ev['currency']:3} {ev['name'][:50]}")
