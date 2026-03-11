import os
import sys
from dotenv import load_dotenv
import pyotp

load_dotenv()

from NorenRestApiPy.NorenApi import NorenApi

# Setup API
SHOONYA_HOST = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
WS_HOST = SHOONYA_HOST.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP/")
api = NorenApi(host=SHOONYA_HOST, websocket=WS_HOST)

# Login
USER = os.getenv("SHOONYA_USER")
PWD = os.getenv("SHOONYA_PWD")
FACTOR2 = os.getenv("SHOONYA_FACTOR2")
VC = os.getenv("SHOONYA_VC")
APP_KEY = os.getenv("SHOONYA_APP_KEY")
IMEI = os.getenv("SHOONYA_IMEI")

totp = pyotp.TOTP(FACTOR2).now()
res = api.login(userid=USER, password=PWD, twoFA=totp, vendor_code=VC, api_secret=APP_KEY, imei=IMEI)

if res and res.get("stat") == "Ok":
    print("Login successful.")
    # Search SENSEX
    search_res = api.searchscrip(exchange="BSE", searchtext="SENSEX")
    print("SENSEX results:")
    for item in search_res:
        if "SENSEX" in item.get('tsym', ''):
            print(item)
else:
    print("Login failed:", res)
