import os
import pyotp
from dotenv import load_dotenv
from NorenRestApiPy.NorenApi import NorenApi
import logging

logging.basicConfig(level=logging.INFO)

def test_login():
    load_dotenv('.env')
    print("Testing Shoonya API Login with dynamic TOTP...")
    
    # Init NorenApi
    api = NorenApi(host='https://api.shoonya.com/NorenWClientTP/', websocket='wss://api.shoonya.com/NorenWSTP/')
    
    # Generate factor2
    factor2_secret = os.getenv('SHOONYA_FACTOR2')
    dynamic_factor2 = pyotp.TOTP(factor2_secret).now()
    print(f"Generated TOTP: {dynamic_factor2}")
    
    # Attempt login
    res = api.login(
        userid=os.getenv('SHOONYA_USER'),
        password=os.getenv('SHOONYA_PWD'),
        twoFA=dynamic_factor2,
        vendor_code=os.getenv('SHOONYA_VC'),
        api_secret=os.getenv('SHOONYA_APP_KEY'),
        imei=os.getenv('SHOONYA_IMEI')
    )
    
    print("\n--- Shoonya Login Response ---")
    print(res)
    print("------------------------------")
    
    if res and res.get('stat') == 'Ok':
        print("\n✅ LOGIN SUCCESSFUL! The credentials and TOTP generator are correct.")
    else:
        print("\n❌ LOGIN FAILED! Please check the credentials.")

if __name__ == '__main__':
    test_login()
