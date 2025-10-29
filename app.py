# app.py - V37 완전 호환 + Bitget/Bybit 한 줄 입력 지원
from flask import Flask, request, jsonify
import requests
import json
import threading
import os
from datetime import datetime
import hmac
import hashlib

app = Flask(__name__)

# === 한 줄 계정 로드 (Bitget + Bybit) ===
accounts = {}
raw_accounts = os.getenv('EXCHANGE_ACCOUNTS', '')
for line in raw_accounts.strip().split('\n'):
    parts = [p.strip() for p in line.split(',')]
    if len(parts) < 4: continue
    uid, exchange, key, secret = parts[0], parts[1].lower(), parts[2], parts[3]
    passphrase = parts[4] if len(parts) > 4 else ''
    accounts[uid] = {
        'exchange': exchange,
        'key': key,
        'secret': secret,
        'passphrase': passphrase
    }

# === V37 메시지 파싱 ===
def parse_v37(message):
    if not message.startswith('TVM:') or not message.endswith(':MVT'):
        return None
    try:
        json_str = message[4:-4]
        data = json.loads(json_str)

        exchange = data.get('exchange', '').lower()
        account = data.get('account', '')
        symbol = data.get('symbol', '').replace('/', 'USDT')
        side = data.get('side', '')
        bal_pct = float(data.get('bal_pct', 0))
        leverage = int(data.get('leverage', 1))
        margin_type = data.get('margin_type', 'cross')
        token = data.get('token', '')
        trailing_stop = float(data.get('trailing_stop', 0))
        ts_ac_price = float(data.get('ts_ac_price', 0))

        direction = ''
        if 'buy' in side:
            direction = 'open_long' if 'close' not in side else 'close_short'
        elif 'sell' in side:
            direction = 'open_short' if 'close' not in side else 'close_long'
        else:
            return None

        return {
            'exchange': exchange,
            'account': account,
            'symbol': symbol,
            'direction': direction,
            'bal_pct': bal_pct,
            'leverage': leverage,
            'margin_type': margin_type,
            'token': token,
            'trailing_stop': trailing_stop,
            'ts_ac_price': ts_ac_price
        }
    except Exception as e:
        print("Parse error:", e)
        return None

# === Bitget 선물 ===
def place_bitget_futures(data):
    acc = accounts[data['account']]
    if acc['exchange'] != 'bitget': return {"error": "Not Bitget"}

    def sign(method, url, body, ts):
        payload = f"{ts}{method.upper()}{url}{json.dumps(body) if body else ''}"
        return hmac.new(acc['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()

    ts = str(int(datetime.now().timestamp() * 1000))

    # 레버리지
    lev_url = "/api/mix/v1/account/setLeverage"
    lev_body = {"symbol": data['symbol'], "marginCoin": "USDT", "leverage": str(data['leverage'])}
    headers = {
        'ACCESS-KEY': acc['key'],
        'ACCESS-SIGN': sign('POST', lev_url, lev_body, ts),
        'ACCESS-TIMESTAMP': ts,
        'ACCESS-PASSPHRASE': acc['passphrase'],
        'Content-Type': 'application/json'
    }
    requests.post("https://api.bitget.com" + lev_url, headers=headers, json=lev_body)

    # 마진 타입
    if data['margin_type'] == 'isolated':
        margin_url = "/api/mix/v1/account/setMarginMode"
        margin_body = {"symbol": data['symbol'], "marginMode": "isolated"}
        ts2 = str(int(datetime.now().timestamp() * 1000))
        headers2 = {**headers, 'ACCESS-SIGN': sign('POST', margin_url, margin_body, ts2), 'ACCESS-TIMESTAMP': ts2}
        requests.post("https://api.bitget.com" + margin_url, headers=headers2, json=margin_body)

    # 잔고
    bal_res = requests.get("https://api.bitget.com/api/mix/v1/account/accounts", headers={**headers, 'ACCESS-SIGN': sign('GET', '/api/mix/v1/account/accounts', '', ts)}).json()
    usdt_balance = next((x for x in bal_res.get('data', []) if x['marginCoin'] == 'USDT'), {}).get('available', '0')
    price = float(requests.get(f"https://api.bitget.com/api/mix/v1/market/ticker?symbol={data['symbol']}").json()['data']['lastPrice'])
    qty = float(usdt_balance) * data['bal_pct'] / 100 / price

    # 주문
    order_url = "/api/mix/v1/plan/placeOrder"
    body = {
        "symbol": data['symbol'],
        "marginCoin": "USDT",
        "side": data['direction'],
        "orderType": "market",
        "size": str(round(qty, 6)),
        "clientOid": f"v37_{int(datetime.now().timestamp())}"
    }
    if data['trailing_stop'] > 0:
        body['triggerPrice'] = str(round(price + data['ts_ac_price'] if 'long' in data['direction'] else price - data['ts_ac_price'], 6))
        body['callbackRate'] = str(data['trailing_stop'])
    ts3 = str(int(datetime.now().timestamp() * 1000))
    headers3 = {**headers, 'ACCESS-SIGN': sign('POST', order_url, body, ts3), 'ACCESS-TIMESTAMP': ts3}
    return requests.post("https://api.bitget.com" + order_url, headers=headers3, json=body).json()

# === Bybit 선물 (테스트넷/실제넷 자동) ===
def place_bybit_futures(data):
    acc = accounts[data['account']]
    if acc['exchange'] != 'bybit': return {"error": "Not Bybit"}

    base_url = "https://api-testnet.bybit.com" if 'testnet' in acc['key'].lower() else "https://api.bybit.com"
    api_key, secret = acc['key'], acc['secret']

    def bybit_sign(params, ts):
        param_str = f"{api_key}{ts}5000{json.dumps(params) if isinstance(params, dict) else params}"
        return hmac.new(secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()

    ts = str(int(datetime.now().timestamp() * 1000))

    # 레버리지
    lev_params = {
        "category": "linear",
        "symbol": data['symbol'],
        "buyLeverage": str(data['leverage']),
        "sellLeverage": str(data['leverage'])
    }
    sign = bybit_sign(lev_params, ts)
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-SIGN': sign,
        'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-RECV-WINDOW': '5000',
        'Content-Type': 'application/json'
    }
    requests.post(f"{base_url}/v5/position/set-leverage", headers=headers, json=lev_params)

    # 마진 타입
    margin_params = {"category": "linear", "symbol": data['symbol'], "marginMode": data['margin_type']}
    sign_m = bybit_sign(margin_params, ts)
    headers_m = {**headers, 'X-BAPI-SIGN': sign_m}
    requests.post(f"{base_url}/v5/account/set-margin-mode", headers=headers_m, json=margin_params)

    # 잔고
    bal_res = requests.get(f"{base_url}/v5/account/wallet-balance", headers={**headers, 'X-BAPI-SIGN': bybit_sign({"category": "linear"}, ts)}, params={"category": "linear"}).json()
    usdt_balance = next((x for x in bal_res.get('result', {}).get('list', []) if x['coin'] == 'USDT'), {}).get('walletBalance', '0')
    price_res = requests.get(f"{base_url}/v5/market/tickers", params={"category": "linear", "symbol": data['symbol']}).json()
    price = float(price_res['result']['list'][0]['lastPrice'])
    qty = float(usdt_balance) * data['bal_pct'] / 100 / price

    # 주문
    order_params = {
        "category": "linear",
        "symbol": data['symbol'],
        "side": "Buy" if 'buy' in data['side'] else "Sell",
        "orderType": "Market",
        "qty": str(round(qty, 6))
    }
    if data['trailing_stop'] > 0:
        order_params['tpslMode'] = "FullMode"
        order_params['tpTriggerBy'] = "LastPrice"
        order_params['slTriggerBy'] = "LastPrice"
        order_params['slRate'] = str(data['trailing_stop'])

    sign_o = bybit_sign(order_params, ts)
    headers_o = {**headers, 'X-BAPI-SIGN': sign_o}
    return requests.post(f"{base_url}/v5/order/create", headers=headers_o, json=order_params).json()

# === 웹훅 ===
@app.route('/order', methods=['POST'])
def webhook():
    data = request.json or {}
    message = data.get('message', '')
    parsed = parse_v37(message)
    if not parsed or parsed['account'] not in accounts:
        return jsonify({"error": "Invalid"}), 400

    def run():
        exchange = accounts[parsed['account']]['exchange']
        result = place_bybit_futures(parsed) if exchange == 'bybit' else place_bitget_futures(parsed)
        print(f"V37 [{parsed['account']} {exchange}]:", result)

    threading.Thread(target=run).start()
    return jsonify({"status": "주문 전송됨", "account": parsed['account'], "exchange": accounts[parsed['account']]['exchange']}), 200

if __name__ == '__main__':
    app.run()
