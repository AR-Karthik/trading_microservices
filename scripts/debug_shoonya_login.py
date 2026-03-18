"""
Shoonya API Integration Diagnostic Script
Bypasses standard client wrappers to manually execute HTTP POST login requests using raw SHA256 hashing and TOTP generation.
"""
import os
import requests
import pyotp
import json
import hashlib
from dotenv import load_dotenv

def test_raw_login():
    load_dotenv()
    user = os.getenv("SHOONYA_USER")
    pwd = os.getenv("SHOONYA_PWD")
    factor2 = os.getenv("SHOONYA_FACTOR2")
    vc = os.getenv("SHOONYA_VC")
    app_key = os.getenv("SHOONYA_APP_KEY")
    imei = os.getenv("SHOONYA_IMEI")
    
    totp = pyotp.TOTP(factor2).now()
    pwd_sha256 = hashlib.sha256(pwd.encode('utf-8')).hexdigest()
    app_key_sha256 = hashlib.sha256(f"{user}|{app_key}".encode('utf-8')).hexdigest()

    payload = {
        "apkversion": "js:1.0.0",
        "uid": user,
        "pwd": pwd_sha256,
        "factor2": totp,
        "vc": vc,
        "appkey": app_key_sha256,
        "imei": imei,
        "source": "API"
    }

    url = 'https://api.shoonya.com/NorenWClientTP/QuickAuth'
    try:
        data = "jData=" + json.dumps(payload)
        response = requests.post(url, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        print("STATUS:", response.status_code)
        
        with open("shoonya_login_debug.json", "w") as f:
            f.write(response.text)
        print("Response saved to shoonya_login_debug.json")
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    test_raw_login()
