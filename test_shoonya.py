import os
import logging
from dotenv import load_dotenv
from NorenRestApiPy.NorenApi import NorenApi

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ShoonyaTest")

def main():
    load_dotenv()
    
    host = os.getenv("SHOONYA_HOST")
    user = os.getenv("SHOONYA_USER")
    pwd = os.getenv("SHOONYA_PWD")
    factor2 = os.getenv("SHOONYA_FACTOR2")
    vc = os.getenv("SHOONYA_VC")
    app_key = os.getenv("SHOONYA_APP_KEY")
    imei = os.getenv("SHOONYA_IMEI", "dummy123")
    
    # Shoonya websocket URL is typically same host with wss protocol and WSTP path
    ws_url = host.replace('https', 'wss').replace('NorenWClientTP', 'NorenWSTP')
    if not ws_url.endswith('/'):
        ws_url += '/'
        
    logger.info(f"Attempting login for user: {user} on host: {host}")
    
    api = NorenApi(host=host, websocket=ws_url)
    
    try:
        login_response = api.login(
            userid=user, 
            password=pwd, 
            twoFA=factor2, 
            vendor_code=vc, 
            api_secret=app_key, 
            imei=imei
        )
        
        if login_response and login_response.get('stat') == 'Ok':
            logger.info("✅ Login Successful!")
            logger.info(f"Logged in as: {login_response.get('uname')}")
            
            # Try fetching a quote
            # NFO|NIFTY for index is usually NSE|Nifty 50 or similar depending on the exact symbol
            # Let's try to get a quote for a known equity first to be safe
            quote = api.get_quotes(exchange="NSE", token="26000") # 26000 is NIFTY 50 index token in Shoonya usually
            
            if quote and quote.get('stat') == 'Ok':
                logger.info("✅ Quote Fetched Successfully!")
                logger.info(f"NIFTY 50 Last Price: {quote.get('lp')}")
            else:
                logger.error(f"❌ Failed to fetch quote: {quote}")
                
        else:
            logger.error(f"❌ Login Failed: {login_response}")
            
    except Exception as e:
        logger.error(f"❌ Exception during connection: {e}")

if __name__ == "__main__":
    main()
