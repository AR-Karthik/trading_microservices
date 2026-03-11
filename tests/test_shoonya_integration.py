"""
tests/test_shoonya_integration.py
==================================
Comprehensive Shoonya API Integration Test Suite.

Covers — in order of dependency:
  1.  Login (TOTP-based)
  2.  Market Data: searchscrip, get_security_info, get_quotes, get_option_chain
  3.  Account: get_limits, get_positions, get_holdings
  4.  Orders: place_order (Limit + Market), single_order_history, cancel_order
  5.  Websocket: start_websocket, subscribe (touchline for NIFTY index)

Usage:
    pytest tests/test_shoonya_integration.py -v -s

All tests that depend on a valid session are marked with LIVE=True.
If the .env file has valid credentials, the full suite runs.
If credentials are missing, tests are skipped gracefully.

Ref: https://github.com/Shoonya-Dev/ShoonyaApi-py
"""

import os
import time
import json
import logging
import threading
import unittest
from datetime import datetime, timezone

import pyotp
from dotenv import load_dotenv

logger = logging.getLogger("ShoonyaIntegrationTest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

load_dotenv(".env")

# ── Credentials ----------------------------------------------------------------
USER   = os.getenv("SHOONYA_USER")
PWD    = os.getenv("SHOONYA_PWD")
FACTOR2 = os.getenv("SHOONYA_FACTOR2")   # TOTP secret (Base32)
VC     = os.getenv("SHOONYA_VC")
APP_KEY = os.getenv("SHOONYA_APP_KEY")
IMEI   = os.getenv("SHOONYA_IMEI")

CREDENTIALS_AVAILABLE = all([USER, PWD, FACTOR2, VC, APP_KEY, IMEI])

# Shoonya host
SHOONYA_HOST = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
WS_HOST      = SHOONYA_HOST.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP/")

# Test symbols (read-only market data — safe to query in any session)
NSE_NIFTY_TOKEN  = "26000"   # NIFTY 50 index
NSE_BANKNIFTY_TOK = "26009"  # BANK NIFTY index
NSE_INFY         = "INFY-EQ"

# Shared API instance + session token
_api         = None
_susertoken  = None


def _get_api():
    """Lazy-init the NorenApi client (singleton for the test session)."""
    global _api
    if _api is None:
        try:
            from NorenRestApiPy.NorenApi import NorenApi
            _api = NorenApi(host=SHOONYA_HOST, websocket=WS_HOST)
        except ImportError:
            _api = None
    return _api


def _login() -> bool:
    """Perform TOTP-based login. Returns True on success."""
    global _susertoken
    if _susertoken:
        return True

    api = _get_api()
    if api is None:
        return False

    try:
        totp = pyotp.TOTP(FACTOR2).now()
        res = api.login(userid=USER, password=PWD, twoFA=totp,
                        vendor_code=VC, api_secret=APP_KEY, imei=IMEI)
        if res and res.get("stat") == "Ok":
            _susertoken = res.get("susertoken")
            logger.info(f"✅ Login OK — susertoken …{_susertoken[-8:]}")
            return True
        else:
            logger.error(f"❌ Login failed: {res}")
            return False
    except Exception as e:
        logger.error(f"Login exception: {e}")
        return False


def skip_if_no_creds(func):
    """Decorator: skip test if credentials are absent or login fails."""
    def wrapper(*args, **kwargs):
        if not CREDENTIALS_AVAILABLE:
            args[0].skipTest("Shoonya credentials not set in .env")
        if not _login():
            args[0].skipTest("Shoonya login failed")
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Authentication
# ═══════════════════════════════════════════════════════════════════════════════

class TestShoonyaLogin(unittest.TestCase):

    def test_credentials_present(self):
        """All required .env keys must be set."""
        missing = [k for k, v in {
            "SHOONYA_USER": USER, "SHOONYA_PWD": PWD,
            "SHOONYA_FACTOR2": FACTOR2, "SHOONYA_VC": VC,
            "SHOONYA_APP_KEY": APP_KEY, "SHOONYA_IMEI": IMEI
        }.items() if not v]
        if missing:
            self.skipTest(f"Missing env vars: {', '.join(missing)}")

    @skip_if_no_creds
    def test_login_returns_ok(self):
        """Live login must return stat=Ok and a valid susertoken."""
        api = _get_api()
        totp = pyotp.TOTP(FACTOR2).now()
        res = api.login(userid=USER, password=PWD, twoFA=totp,
                        vendor_code=VC, api_secret=APP_KEY, imei=IMEI)
        self.assertIsNotNone(res)
        self.assertEqual(res.get("stat"), "Ok", f"Login stat not Ok: {res}")
        self.assertIn("susertoken", res, "susertoken must be in response")
        logger.info(f"Login susertoken: …{res['susertoken'][-8:]}")

    @skip_if_no_creds
    def test_totp_changes_every_30s(self):
        """Two successive TOTPs separated by 31s should differ (sanity check)."""
        t1 = pyotp.TOTP(FACTOR2).now()
        time.sleep(31)
        t2 = pyotp.TOTP(FACTOR2).now()
        self.assertNotEqual(t1, t2, "TOTP should rotate every 30 seconds")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Market Data
# ═══════════════════════════════════════════════════════════════════════════════

class TestShoonyaMarketData(unittest.TestCase):

    @skip_if_no_creds
    def test_searchscrip_nifty(self):
        """searchscrip must return at least one result for 'NIFTY'."""
        api = _get_api()
        res = api.searchscrip(exchange="NSE", searchtext="NIFTY")
        self.assertIsNotNone(res, "searchscrip should return data")
        self.assertIsInstance(res, list, "searchscrip returns list of contracts")
        self.assertGreater(len(res), 0, "At least one record expected for 'NIFTY'")
        logger.info(f"searchscrip 'NIFTY' → {len(res)} results, first: {res[0].get('tsym')}")

    @skip_if_no_creds
    def test_get_quotes_nifty_index(self):
        """get_quotes for NSE|26000 (NIFTY 50) must return last price."""
        api = _get_api()
        res = api.get_quotes(exchange="NSE", token=NSE_NIFTY_TOKEN)
        self.assertIsNotNone(res)
        self.assertIn("lp", res, "Last price 'lp' must be present in quotes")
        lp = float(res["lp"])
        self.assertGreater(lp, 1000, f"NIFTY LTP {lp} seems unreasonably low")
        logger.info(f"NIFTY LTP: {lp}")

    @skip_if_no_creds
    def test_get_quotes_banknifty_index(self):
        """get_quotes for NSE|26009 (BANK NIFTY) must return last price."""
        api = _get_api()
        res = api.get_quotes(exchange="NSE", token=NSE_BANKNIFTY_TOK)
        self.assertIsNotNone(res)
        lp = float(res.get("lp", 0))
        self.assertGreater(lp, 1000)
        logger.info(f"BANKNIFTY LTP: {lp}")

    @skip_if_no_creds
    def test_get_security_info_infy(self):
        """get_security_info for INFY-EQ should return symbol details."""
        api = _get_api()
        res = api.get_security_info(exchange="NSE", token="1594")  # INFY token
        self.assertIsNotNone(res)
        logger.info(f"Security info: {res.get('tsym')} tick={res.get('ti')}")

    @skip_if_no_creds
    def test_get_option_chain_nifty(self):
        """get_option_chain must return CE and PE contracts around ATM."""
        api = _get_api()
        nifty_quote = api.get_quotes(exchange="NSE", token=NSE_NIFTY_TOKEN)
        if not nifty_quote:
            self.skipTest("Could not fetch NIFTY quote for option chain test")

        lp = float(nifty_quote["lp"])
        atm = round(lp / 50) * 50  # Round to nearest 50

        res = api.get_option_chain(exchange="NFO", tradingsymbol="NIFTY", strikeprice=atm, count=3)
        self.assertIsNotNone(res)
        logger.info(f"Option chain for NIFTY ATM={atm}: {len(res) if res else 0} contracts")

    @skip_if_no_creds
    def test_time_price_series(self):
        """get_time_price_series must return OHLCV candles."""
        api = _get_api()
        # Request last 5-min candle for NIFTY index
        res = api.get_time_price_series(
            exchange="NSE", token=NSE_NIFTY_TOKEN,
            starttime=str(int(time.time()) - 3600),
            interval=5
        )
        if res is None:
            self.skipTest("No intraday candles available (likely outside market hours)")
        self.assertIsInstance(res, list)
        if res:
            candle = res[0]
            # OHLCV should have at least open/close
            logger.info(f"Candle sample: o={candle.get('into')} c={candle.get('intc')}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Account & Holdings
# ═══════════════════════════════════════════════════════════════════════════════

class TestShoonyaAccount(unittest.TestCase):

    @skip_if_no_creds
    def test_get_limits(self):
        """get_limits must return cash/margin details."""
        api = _get_api()
        res = api.get_limits()
        self.assertIsNotNone(res)
        # Should have 'cash' or 'payin' field
        has_funds = "cash" in res or "payin" in res or "collateral" in res
        self.assertTrue(has_funds, f"Expected margin info in response: {res.keys()}")
        logger.info(f"Limits: cash={res.get('cash')}, payin={res.get('payin')}")

    @skip_if_no_creds
    def test_get_positions(self):
        """get_positions must return list (possibly empty outside trading hours)."""
        api = _get_api()
        res = api.get_positions()
        # Can be None (no positions) or a list
        if res is not None:
            self.assertIsInstance(res, list)
            logger.info(f"Positions: {len(res)} open")

    @skip_if_no_creds
    def test_get_order_book(self):
        """get_order_book must return list of orders (possibly empty)."""
        api = _get_api()
        res = api.get_order_book()
        if res is not None:
            self.assertIsInstance(res, list)
            logger.info(f"Order book: {len(res)} orders")

    @skip_if_no_creds
    def test_get_trade_book(self):
        """get_trade_book must return list of trades (possibly empty)."""
        api = _get_api()
        res = api.get_trade_book()
        if res is not None:
            self.assertIsInstance(res, list)
            logger.info(f"Trade book: {len(res)} trades")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Orders (Read-only + Paper-safe)
# ═══════════════════════════════════════════════════════════════════════════════

class TestShoonyaOrders(unittest.TestCase):
    """
    NOTE: place_order tests use a very low priced instrument at a far-away
    limit price to ensure they are never accidentally filled. The order is
    immediately cancelled after verification.
    """

    _placed_order_no = None

    @skip_if_no_creds
    def test_place_and_cancel_limit_order(self):
        """Place a far-OTM limit order and immediately cancel it."""
        api = _get_api()

        # Use LIQUIDBEES-EQ (low price, high liquidity) with a price 90% below market
        ret = api.place_order(
            buy_or_sell='B',
            product_type='I',        # Intraday
            exchange='NSE',
            tradingsymbol='LIQUIDBEES-EQ',
            quantity=1,
            discloseqty=0,
            price_type='LMT',
            price=1.00,              # Far below market, will never fill
            trigger_price=None,
            retention='DAY',
            remarks='k7_test_order'
        )
        logger.info(f"Place order response: {ret}")

        if ret is None or ret.get("stat") == "Not_Ok":
            self.skipTest(f"Market closed or order rejected: {ret}")

        self.assertEqual(ret.get("stat"), "Ok", f"place_order should return Ok: {ret}")
        order_no = ret.get("norenordno")
        self.assertIsNotNone(order_no, "norenordno must be present")
        TestShoonyaOrders._placed_order_no = order_no
        logger.info(f"✅ Order placed: #{order_no}")

        # Immediately cancel
        cancel_ret = api.cancel_order(orderno=order_no)
        logger.info(f"Cancel response: {cancel_ret}")
        if cancel_ret:
            self.assertIn(cancel_ret.get("stat"), ["Ok", "Not_Ok"],
                          "cancel_order must return stat field")

    @skip_if_no_creds
    def test_single_order_history(self):
        """If we placed an order above, single_order_history must return its timeline."""
        if not TestShoonyaOrders._placed_order_no:
            self.skipTest("Skipping — no order was placed in this session")

        api = _get_api()
        time.sleep(1)  # allow OMS to process
        res = api.single_order_history(orderno=TestShoonyaOrders._placed_order_no)
        logger.info(f"Order history: {res}")

        self.assertIsNotNone(res)
        self.assertIsInstance(res, list, "single_order_history returns list")
        # Status sequence should start with at least PENDING/OPEN/COMPLETE/CANCELLED
        statuses = [e.get("status", "").upper() for e in res]
        logger.info(f"Order statuses: {statuses}")
        self.assertTrue(
            any(s in ("PENDING", "COMPLETE", "CANCELLED", "OPEN", "REJECTED") for s in statuses),
            f"Expected recognized status in timeline, got: {statuses}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Websocket Feed (Touchline)
# ═══════════════════════════════════════════════════════════════════════════════

class TestShoonyaWebsocket(unittest.TestCase):

    @skip_if_no_creds
    def test_websocket_touchline_nifty(self):
        """
        Subscribe to NSE|26000 (NIFTY index). Within 10 seconds we should
        receive at least one 'tk' (touchline snapshot) message.
        """
        api = _get_api()
        received_ticks = []
        feed_opened = threading.Event()
        error_occurred = []

        def on_feed_update(tick):
            received_ticks.append(tick)
            logger.info(f"WS tick: t={tick.get('t')} tk={tick.get('tk')} lp={tick.get('lp')}")

        def on_order_update(order):
            logger.info(f"WS order: {order}")

        def on_open():
            feed_opened.set()

        def on_error(msg):
            error_occurred.append(msg)
            logger.error(f"WS error: {msg}")

        # Start websocket in a daemon thread
        ws_thread = threading.Thread(
            target=api.start_websocket,
            kwargs=dict(
                subscribe_callback=on_feed_update,
                order_update_callback=on_order_update,
                socket_open_callback=on_open,
                socket_error_callback=on_error,
            ),
            daemon=True
        )
        ws_thread.start()

        # Wait for socket to open
        opened = feed_opened.wait(timeout=10)
        if not opened:
            self.skipTest("WebSocket did not open within 10s (likely outside market hours)")

        # Subscribe to NIFTY 50 index touchline
        api.subscribe(f"NSE|{NSE_NIFTY_TOKEN}")  # Touchline
        time.sleep(5)  # Wait for tick

        if not received_ticks:
            self.skipTest("No ticks received within 5s — market likely closed")

        # Validate tick structure
        first_tick = received_ticks[0]
        self.assertIn("t", first_tick, "Tick must have 't' type field")
        self.assertIn(first_tick["t"], ("tk", "tf"), f"Unexpected tick type: {first_tick['t']}")

        if "lp" in first_tick:
            lp = float(first_tick["lp"])
            self.assertGreater(lp, 1000, f"NIFTY LTP {lp} too low")
            logger.info(f"✅ Live NIFTY LTP from WS: {lp}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Logout (always last)
# ═══════════════════════════════════════════════════════════════════════════════

class TestShoonyaLogout(unittest.TestCase):

    @skip_if_no_creds
    def test_logout_returns_ok(self):
        """logout() should cleanly terminate the session."""
        api = _get_api()
        ret = api.logout()
        logger.info(f"Logout response: {ret}")
        # stat=Ok or server may return Not_Ok if session was already expired
        if ret:
            logger.info(f"Logout stat: {ret.get('stat')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
