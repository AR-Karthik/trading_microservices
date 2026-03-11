import os, sys, pyotp
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('.env')

USER = os.getenv('SHOONYA_USER')
PWD = os.getenv('SHOONYA_PWD')
VC = os.getenv('SHOONYA_VC')
APP_KEY = os.getenv('SHOONYA_APP_KEY')
FACTOR2_SECRET = os.getenv('SHOONYA_FACTOR2', '')
IMEI = os.getenv('SHOONYA_IMEI', 'abc1234')

try:
    totp = pyotp.TOTP(FACTOR2_SECRET).now()
    print(f'User: {USER}, Generating TOTP with secret [{FACTOR2_SECRET[:5]}...] -> [{totp}]')
except Exception as e:
    print('Failed to generate TOTP from secret:', e)
    totp = '000000'

print('Init NorenApi...')
try:
    from NorenRestApiPy.NorenApi import NorenApi
    api = NorenApi(
        host='https://api.shoonya.com/NorenWClientTP/',
        websocket='wss://api.shoonya.com/NorenWSTP/'
    )
except ImportError:
    print('NorenRestApiPy not found. Make sure it is installed.')
    sys.exit(1)

print('Sending login...')
try:
    res = api.login(
        userid=USER,
        password=PWD,
        twoFA=totp,
        vendor_code=VC,
        api_secret=APP_KEY,
        imei=IMEI
    )
    print('Response Stat:', res.get('stat') if res else 'None')
    
    if res and res.get('stat') == 'Not_Ok':
        print('Error Message:', res.get('emsg'))
except Exception as e:
    print('Login Exception:', e)
    sys.exit(1)

if res and res.get('stat') == 'Ok':
    print('SUCCESS! Fetching NSE NIFTY...')
    try:
        q = api.get_quotes(exchange='NSE', token='26000')
        print(f"NIFTY50 Quote - LTP: {q.get('lp')}, High: {q.get('h')}, Low: {q.get('l')}")
    except Exception as e:
        print('Failed to fetch quote:', e)
