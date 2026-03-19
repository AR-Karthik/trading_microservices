import httpx
import json
import time
import os
import asyncio
from datetime import datetime
import redis
from core.logger import setup_logger

logger = setup_logger("FIIDIIFetcher", log_file="logs/fii_dii.log")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")

class FIIDIIFetcher:
    def __init__(self):
        self.redis = redis.Redis(host=REDIS_HOST, port=6379, db=0, decode_responses=True)
        self.url = "https://www.nseindia.com/api/fiidiiTradeDetails"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/reports/fii-dii"
        }
        self.session = httpx.AsyncClient(headers=self.headers, timeout=10.0)

    async def fetch_data(self):
        try:
            # Prime session state before attempting authenticated target URLs natively
            await self.session.get("https://www.nseindia.com", follow_redirects=True)
            await asyncio.sleep(1) # NSE needs a moment to set cookies
            
            response = await self.session.get(self.url)
            if response.status_code == 200:
                data = response.json()
                # Publish the latest authoritative data for subscriber visualization
                self.redis.set("latest_fii_dii", json.dumps(data))
                self.redis.set("LAST_FII_DII_FETCH", datetime.now().isoformat())
                logger.info("Successfully fetched FII/DII data")
                return data
            else:
                logger.error(f"Failed to fetch FII/DII data: {response.status_code}")
                # Present fallback testing structures defensively
                return self._get_mock_data()
        except Exception as e:
            logger.error(f"Error fetching FII/DII data: {str(e)}")
            return self._get_mock_data()

    def _get_mock_data(self):
        return [
            {"category": "FII", "buyValue": 12500.45, "sellValue": 11200.30, "netValue": 1300.15, "date": datetime.now().strftime("%d-%b-%Y")},
            {"category": "DII", "buyValue": 8500.12, "sellValue": 9200.45, "netValue": -700.33, "date": datetime.now().strftime("%d-%b-%Y")}
        ]

    async def run(self):
        logger.info("Starting FII/DII Fetcher (Running every 30 mins during market hours)")
        while True:
            # Gate fetching intervals strictly against primary active banking hours
            now = datetime.now()
            if 9 <= now.hour <= 16:
                await self.fetch_data()
            
            # Impose fixed polling delay enforcing exchange rate-limits natively
            await asyncio.sleep(1800)

if __name__ == "__main__":
    fetcher = FIIDIIFetcher()
    asyncio.run(fetcher.run())
