from NorenRestApiPy.NorenApi import NorenApi
import inspect

api = NorenApi(host="http://dummy", websocket="ws://dummy")
print("Signature:", inspect.signature(api.get_option_chain))
print("Dir:", dir(api))
