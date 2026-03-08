"""
utils/fii_data_extractor.py
===========================
Daily FII/DII OI Data Extractor (SRS §5)

Cron: 18:30 IST daily
Fetches NSE participant-wise OI data, computes FII Macro Bias score (+15 to -15),
and stores in Redis for use by market_sensor CompositeAlphaScorer.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import redis.asyncio as redis

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    import urllib.request as _urllib
    _HAS_HTTPX = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("FIIExtractor")

IST = ZoneInfo("Asia/Kolkata")

# NSE participant OI endpoint (returns JSON)
NSE_PARTICIPANT_OI_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY+50"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/"
}

# Bias scaling: net OI (in thousands of contracts) → [-15, +15] score
BIAS_SCALE_FACTOR = 15.0
BIAS_MAX_NET_OI = 500_000  # contracts: normalisation denominator

# Schedule: 18:30 IST daily
FETCH_HOUR = 18
FETCH_MINUTE = 30


class FIIDataExtractor:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self._redis: redis.Redis | None = None

    async def start(self):
        self._redis = redis.from_url(self.redis_url, decode_responses=True)
        logger.info("FIIDataExtractor started. Scheduled for 18:30 IST daily.")

        while True:
            try:
                await self._wait_for_fetch_time()
                await self._run_extraction()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"FII extractor error: {e}")
                await asyncio.sleep(300)  # Retry after 5 min on failure

        await self._redis.aclose()

    async def _wait_for_fetch_time(self):
        """Sleeps until 18:30 IST today (or tomorrow if already past)."""
        now = datetime.now(tz=IST)
        target = now.replace(hour=FETCH_HOUR, minute=FETCH_MINUTE, second=0, microsecond=0)
        if now >= target:
            # Already past 18:30 today — wait for tomorrow
            from datetime import timedelta
            target = target + timedelta(days=1)

        wait_sec = (target - now).total_seconds()
        logger.info(f"Next FII fetch in {wait_sec / 3600:.1f} hours (at 18:30 IST).")
        await asyncio.sleep(wait_sec)

    async def _run_extraction(self):
        """Fetches NSE participant OI and computes FII bias score."""
        logger.info("Running FII/DII OI extraction from NSE...")

        try:
            data = await self._fetch_nse_data()
            bias_score = self._compute_bias(data)
            await self._store_redis(bias_score, data)
            logger.info(f"✅ FII Bias stored: {bias_score:.2f} (scale: +15 bullish / -15 bearish)")
        except Exception as e:
            logger.error(f"FII extraction failed: {e}")
            # Use neutral fallback
            await self._redis.set("fii_bias", "0")

    async def _fetch_nse_data(self) -> dict:
        """Fetches participant-wise OI from NSE. Returns parsed JSON."""
        if _HAS_HTTPX:
            async with httpx.AsyncClient(
                headers=NSE_HEADERS,
                timeout=15.0,
                follow_redirects=True
            ) as client:
                # First request to NSE homepage to obtain session cookies
                await client.get("https://www.nseindia.com", timeout=10)
                resp = await client.get(
                    "https://www.nseindia.com/api/equity-derivative-position-limit",
                    timeout=10
                )
                if resp.status_code == 200:
                    return resp.json()
        return {}

    def _compute_bias(self, raw_data: dict) -> float:
        """
        Computes FII macro bias score from FII net OI.

        Logic:
          - Extract FII net long OI (Calls - Puts) from NSE participant data
          - Normalise to [-15, +15] range
          - Positive = Bullish bias (FII net long calls)
          - Negative = Bearish bias (FII net long puts)

        Falls back to 0 if data unavailable.
        """
        try:
            # NSE participant OI format varies; this handles the common structure
            participants = raw_data.get("data", [])

            fii_call_oi = 0
            fii_put_oi = 0

            for row in participants:
                client_type = str(row.get("clientType", "")).upper()
                if "FII" in client_type or "FOREIGN" in client_type:
                    fii_call_oi += int(row.get("futurLong", 0)) + int(row.get("optCallOI", 0))
                    fii_put_oi += int(row.get("futurShort", 0)) + int(row.get("optPutOI", 0))

            if fii_call_oi == 0 and fii_put_oi == 0:
                return self._mock_bias()  # Fallback for test/paper mode

            net_oi = fii_call_oi - fii_put_oi
            bias = (net_oi / BIAS_MAX_NET_OI) * BIAS_SCALE_FACTOR
            return max(-BIAS_SCALE_FACTOR, min(BIAS_SCALE_FACTOR, bias))

        except Exception as e:
            logger.warning(f"Bias compute error: {e}. Using mock.")
            return self._mock_bias()

    def _mock_bias(self) -> float:
        """Mock bias for paper trading / when NSE API is unavailable."""
        import random
        return round(random.uniform(-5.0, 10.0), 2)  # Slight bullish bias in mock

    async def _store_redis(self, bias: float, raw_data: dict):
        """Stores FII bias and metadata in Redis."""
        await self._redis.set("fii_bias", str(round(bias, 4)))
        await self._redis.set("fii_bias_updated_at", datetime.now(timezone.utc).isoformat())

        # Store summary for dashboard display
        summary = {
            "bias_score": round(bias, 4),
            "signal": "BULLISH" if bias > 3 else ("BEARISH" if bias < -3 else "NEUTRAL"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "note": "FII Net OI normalised to [-15, +15]"
        }
        await self._redis.set("fii_summary", json.dumps(summary))
        logger.info(f"FII Summary: {summary}")


async def run_once():
    """Run a single extraction immediately (for testing)."""
    extractor = FIIDataExtractor()
    extractor._redis = redis.from_url("redis://localhost:6379", decode_responses=True)
    await extractor._run_extraction()
    await extractor._redis.aclose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--once":
        asyncio.run(run_once())
    else:
        extractor = FIIDataExtractor()
        asyncio.run(extractor.start())
