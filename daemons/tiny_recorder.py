import os
import time
import json
import logging
import asyncio
import struct
import lzma
from datetime import datetime
from dotenv import load_dotenv
import requests

from core.api_client import ShoonyaApiClient
from NorenRestApiPy.NorenApi import NorenApi

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('tiny_recorder')

load_dotenv()

# Environment variables
USER = os.getenv('SHOONYA_USER')
PWD = os.getenv('SHOONYA_PWD')
FACTOR2 = os.getenv('SHOONYA_FACTOR2')
VC = os.getenv('SHOONYA_VC')
API_KEY = os.getenv('SHOONYA_API_KEY')
IMEI = os.getenv('SHOONYA_IMEI')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GCS_BUCKET_NAME = os.environ.get('GCS_BUCKET_NAME', 'sanctum_bucket')

DATA_DIR = "./data/tick_archive"
os.makedirs(DATA_DIR, exist_ok=True)


class ShoonyaWebsocket(NorenApi):
    def __init__(self, host, websocket, eodhost=''):
        super().__init__(host=host, websocket=websocket, eodhost=eodhost)

    def log_message(self, message, is_error=False):
        if is_error:
            logger.error(f"ShoonyaWS Error: {message}")
        else:
            logger.debug(f"ShoonyaWS Info: {message}")


class TinyRecorder:
    def __init__(self):
        self.api = ShoonyaWebsocket(host='https://api.shoonya.com/NorenWClientTP/',
                                    websocket='wss://api.shoonya.com/NorenWSTP/')
        self.connected = False
        
        # Sensex Power 5 and Nifty/BN Native Tokens
        # Important: BSE token mapping needs to be accurate in practice. Using placeholders like BSE:HDFCBANK.
        self.symbols = [
            "NSE|26000", # NIFTY 50
            "NSE|26009", # NIFTY BANK
            "BSE|1",     # SENSEX
            "NSE|2885",  # RELIANCE
            "NSE|1333",  # HDFCBANK 
            "NSE|4963",  # ICICIBANK
            "NSE|1594",  # INFY
            "NSE|11536", # TCS
            "NSE|5900",  # AXISBANK
            "NSE|1922",  # KOTAKBANK
            "NSE|3045",  # SBIN
            "BSE|500180", # BSE:HDFCBANK
            "BSE|500325", # BSE:RELIANCE
            "BSE|532174", # BSE:ICICIBANK
            "BSE|532454", # BSE:BHARTIARTL
            "BSE|500510", # BSE:LT
        ]
        
        date_str = datetime.now().strftime('%Y%m%d')
        self.binary_file_path = os.path.join(DATA_DIR, f"tbt_{date_str}.bin.lzma")
        self.file_handle = None
        
        # Reconnection settings
        self.max_retries = 10
        self.retry_count = 0

    def send_telegram_message(self, message):
        """Sends a Telegram alert securely."""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("Telegram credentials missing. Skipping message.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Failed to send telegram message: {e}")

    def compress_tick(self, tick_data):
        """
        Compresses a dict tick into extremely minimal packed binary structure.
        Format: timestamp(double) + exchange_id(int8) + token(int32) + last_price(float) + volume(int32)
        """
        try:
            if 'lp' not in tick_data:
                return None
            
            ts = time.time() # t_receive
            # Determine exchange
            e_id = 1 if tick_data.get('e') == 'NSE' else 2 # 1=NSE, 2=BSE
            token = int(tick_data.get('tk', 0))
            lp = float(tick_data.get('lp', 0.0))
            vol = int(tick_data.get('v', 0))
            
            # Pack into binary struct. >BdIfl : big-endian, byte, double, uint32, float, int32
            return struct.pack('>B d I f i', e_id, ts, token, lp, vol)
        except Exception as e:
            logger.error(f"Failed to compress tick: {e}")
            return None

    def on_message(self, message):
        """Websocket handle incoming tick."""
        if self.file_handle:
            binary_data = self.compress_tick(message)
            if binary_data:
                self.file_handle.write(binary_data)

    def on_open(self):
        """Websocket connection established."""
        logger.info("Shoonya WebSocket connected. Subscribing to tokens...")
        self.connected = True
        self.retry_count = 0
        
        # Subscribe
        for symbol in self.symbols:
            self.api.subscribe(symbol)
            logger.info(f"Subscribed: {symbol}")

    def on_error(self, message):
        logger.error(f"Shoonya WebSocket error: {message}")

    def on_close(self, message):
        logger.warning(f"Shoonya WebSocket connection closed: {message}")
        self.connected = False

    def login(self):
        """Login to Shoonya APIs"""
        logger.info("Logging into Shoonya...")
        try:
            res = self.api.login(userid=USER, password=PWD, twoFA=FACTOR2, vendor_code=VC, api_secret=API_KEY, imei=IMEI)
            if res and res.get('stat') == 'Ok':
                logger.info("Login successful.")
                return True
            else:
                logger.error(f"Login failed: {res}")
                return False
        except Exception as e:
            logger.error(f"Login exception: {e}")
            return False

    async def connect_and_stream(self):
        """Main connection and reconnection loop."""
        self.send_telegram_message("🔴 <b>Tiny Recorder Started</b>\nSpinning up e2-micro telemetry.")
        
        # Open LZMA Compressed Binary file
        self.file_handle = lzma.open(self.binary_file_path, "ab")
        
        while True:
            if not self.connected:
                if self.login():
                    logger.info("Starting WebSocket...")
                    self.api.start_websocket(
                        subscribe_callback=self.on_message,
                        socket_open_callback=self.on_open,
                        socket_close_callback=self.on_close,
                        socket_error_callback=self.on_error
                    )
                else:
                    self.retry_count += 1
                    wait_time = min(2 ** self.retry_count, 60)
                    logger.info(f"Retrying login in {wait_time} seconds (Attempt {self.retry_count})")
                    await asyncio.sleep(wait_time)
                    
            # Heartbeat & Time Checks
            now = datetime.now()
            
            # Send live 09:16 heartbeat
            if now.hour == 9 and now.minute == 16 and now.second < 5:
                self.send_telegram_message("🟢 <b>K.A.R.T.H.I.K. Tiny Recorder</b>\nLive ingest of Tri-Brain market DNA active.")
                await asyncio.sleep(5)
                
            # GCS Push / Shutdown condition at 15:45
            if now.hour == 15 and now.minute >= 45:
                logger.info("Market Closed. Initiating GCS Handoff.")
                self.push_to_gcs()
                break
                
            await asyncio.sleep(1)

    def push_to_gcs(self):
        """Uploads the binary log to Google Cloud Storage (GCS)."""
        logger.info(f"Pushing {self.binary_file_path} to GCS Bucket: {GCS_BUCKET_NAME}")
        
        if self.file_handle:
            self.file_handle.close()
            
        try:
            # Using gsutil as an os command for simplicity on linux
            import subprocess
            cmd = f"gsutil cp {self.binary_file_path} gs://{GCS_BUCKET_NAME}/tbt_logs/"
            subprocess.run(cmd, shell=True, check=True)
            self.send_telegram_message(f"📦 <b>GCS Handoff Complete</b>\nFile: {os.path.basename(self.binary_file_path)} pushed successfully to data lake.")
            logger.info("GCS Push Complete.")
            
        except Exception as e:
            logger.error(f"Failed to push to GCS: {e}")
            self.send_telegram_message(f"‼️ <b>GCS Handoff Failed</b>\nError pushing {os.path.basename(self.binary_file_path)}.")


if __name__ == "__main__":
    logger.info("Starting Tiny Recorder.")
    recorder = TinyRecorder()
    asyncio.run(recorder.connect_and_stream())
