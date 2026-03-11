"""
Shoonya API connection test.
Run: python tmp/shoonya_connection_test.py

Tests:
1. Library available
2. Login with SHA256 password
3. Fetch NIFTY50 quote (NSE token 26000)
4. Fetch BANKNIFTY quote (NSE token 26009)
"""
import os, sys, hashlib, json, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

USER    = os.getenv('SHOONYA_USER', '')
PWD     = os.getenv('SHOONYA_PWD', '')
VC      = os.getenv('SHOONYA_VC', '')
APP_KEY = os.getenv('SHOONYA_APP_KEY', '')
IMEI    = os.getenv('SHOONYA_IMEI', 'abc1234')
HOST    = 'https://api.shoonya.com/NorenWClientTP'

print("=" * 50)
print("  K.A.R.T.H.I.K. v0.9 — Shoonya API Test")
print("=" * 50)
print(f"User ID : {USER}")
print(f"VC      : {VC}")
print(f"Host    : {HOST}")
print()

# Step 1: library check
try:
    from NorenRestApiPy.NorenApi import NorenApi
    print("[OK] NorenRestApiPy found")
except ImportError:
    print("[FAIL] NorenRestApiPy not installed — run: pip install NorenRestApiPy")
    sys.exit(1)

# Step 2: Login
pwd_hash = hashlib.sha256(PWD.encode()).hexdigest()
app_hash = hashlib.sha256(f"{USER}|{APP_KEY}".encode()).hexdigest()

# Shoonya QuickAuth format (direct HTTP POST)
payload = {
    'j_data': json.dumps({
        'apkversion': 'js:1.0.0',
        'uid':        USER,
        'pwd':        pwd_hash,
        'factor2':    PWD,        # Finvasia: factor2 = plain password for API users
        'vc':         VC,
        'appkey':     app_hash,
        'imei':       IMEI,
        'source':     'API'
    })
}

print("Attempting login...")
try:
    r = requests.post(f'{HOST}/QuickAuth', data=payload, timeout=15)
    resp = r.json() if r.headers.get('content-type','').startswith('application') else {'raw': r.text}
    print(f"HTTP {r.status_code}")
    print(f"Response: {json.dumps(resp, indent=2)[:600]}")
except Exception as e:
    print(f"[FAIL] Login request error: {e}")
    sys.exit(1)

if resp.get('stat') != 'Ok':
    print(f"\n[FAIL] Login failed: {resp.get('emsg', 'No error message')}")
    print("\nNote: Shoonya may require a TOTP secret (SHOONYA_FACTOR2) in .env if this is a TOTP-enabled account.")
    sys.exit(1)

token = resp.get('susertoken')
print(f"\n[OK] Login successful! Session token: {token[:20]}...")

# Step 3: Fetch NIFTY50 quote
print("\nFetching NIFTY50 (NSE:26000)...")
headers = {'Content-Type': 'application/x-www-form-urlencoded'}
quote_payload = {
    'j_data': json.dumps({'uid': USER, 'exch': 'NSE', 'token': '26000'}),
    'jKey': token
}
try:
    qr = requests.post(f'{HOST}/GetQuotes', data=quote_payload, timeout=10)
    q = qr.json()
    print(f"NIFTY50 LTP : {q.get('lp', 'N/A')}")
    print(f"NIFTY50 High: {q.get('h', 'N/A')}")
    print(f"NIFTY50 Low : {q.get('l', 'N/A')}")
    print(f"NIFTY50 Vol : {q.get('v', 'N/A')}")
except Exception as e:
    print(f"[FAIL] Quote fetch error: {e}")

# Step 4: Fetch BANKNIFTY quote
print("\nFetching BANKNIFTY (NSE:26009)...")
try:
    bq_payload = {
        'j_data': json.dumps({'uid': USER, 'exch': 'NSE', 'token': '26009'}),
        'jKey': token
    }
    bqr = requests.post(f'{HOST}/GetQuotes', data=bq_payload, timeout=10)
    bq = bqr.json()
    print(f"BANKNIFTY LTP : {bq.get('lp', 'N/A')}")
    print(f"BANKNIFTY High: {bq.get('h', 'N/A')}")
    print(f"BANKNIFTY Low : {bq.get('l', 'N/A')}")
except Exception as e:
    print(f"[FAIL] BANKNIFTY quote error: {e}")

print("\n" + "=" * 50)
print("  Shoonya API Connection Test COMPLETE")
print("=" * 50)
