# app.py - CHARTIBOT_V37.1 완전 호환 (계정1/2, 레버리지, 마진타입, 잔고%, 트레일링, 청산)
from flask import Flask, request, jsonify
import requests
import json
import threading
import os
from datetime import datetime
import hmac
import hashlib

app = Flask(__name__)

# === 한 줄 계정 로드 ===
accounts = {}
raw_accounts = os.getenv('BITGET_ACCOUNTS', '')
for line in raw_accounts.strip().split('\n'):
    parts = line.strip().split(',')
    if len(parts) != 4: continue
    uid, key, secret, passphrase = parts
    accounts[uid] = {'key': key, 'secret': secret, 'passphrase': passphrase}

# === 메시지 파싱 (V37 완전 호환) ===
def parse_v37(message):
    if not message.startswith('TVM:') or not message.endswith(':MVT'):
        return None
    try:
        json_str = message[4:-4]
        data = json.loads(json_str)

        exchange = data.get('exchange', '').lower()
        if exchange != 'bitget': return None

        account = data.get('account', '')
        symbol = data.get('symbol', '').replace('/', 'USDT')
        side = data.get('side', '')  # buy / sell
        bal_pct = float(data.get('bal_pct', 0))
        leverage = int(data.get('leverage', 1))
        margin_type = data.get('margin_type', 'cross')
        token = data.get('token', '')
        same_order = data.get('same_order', '')
        position_close = data.get('position_close', False)
        trailing_stop = data.get('trailing_stop', 0)
        ts_ac_price = data.get('ts_ac_price', 0)

        # side 변환
        if 'buy' in side:
            direction = 'open_long' if 'close' not in side else 'close_short'
        elif 'sell' in side:
            direction = 'open_short' if 'close' not in side else 'close_long'
        else:
            return None

        return {
            'account': account,
            'symbol': symbol,
            'direction': direction,
            'bal_pct': bal_pct,
            'leverage': leverage,
            'margin_type': margin_type,
            'token': token,
            'position_close': position_close,
            'trailing_stop': trailing_stop,
            'ts_ac_price': ts_ac_price
        }
    except Exception as e:
        print("Parse error:", e)
        return None

# === Bitget 선물 주문 ===
def place_bitget_futures(data):
    if data['account'] not in accounts:
        return {"error": "Account not found"}
    acc = accounts[data['account']]

    def sign(method, url, body, ts):
        payload = f"{ts}{method.upper()}{url}{json.dumps(body) if body else ''}"
        return hmac.new(acc['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()

    ts = str(int(datetime.now().timestamp() * 1000))

    # 1. 레버리지 설정
    lev_url = "/api/mix/v1/account/setLeverage"
    lev_body = {
        "symbol": data['symbol'],
        "marginCoin": "USDT",
        "leverage": str(data['leverage']),
        "holdSide": "long" if 'open_long' in data['direction'] else "short"
    }
    headers = {
        'ACCESS-KEY': acc['key'],
        'ACCESS-SIGN': sign('POST', lev_url, lev_body, ts),
        'ACCESS-TIMESTAMP': ts,
        'ACCESS-PASSPHRASE': acc['passphrase'],
        'Content-Type': 'application/json'
    }
    requests.post("https://api.bitget.com" + lev_url, headers=headers, json=lev_body)

    # 2. 마진 타입 설정
    if data['margin_type'] == 'isolated':
        margin_url = "/api/mix/v1/account/setMarginMode"
        margin_body = {"symbol": data['symbol'], "marginMode": "isolated"}
        ts2 = str(int(datetime.now().timestamp() * 1000))
        headers2 = {**headers, 'ACCESS-SIGN': sign('POST', margin_url, margin_body, ts2), 'ACCESS-TIMESTAMP': ts2}
        requests.post("https://api.bitget.com" + margin_url, headers=headers2, json=margin_body)

    # 3. 잔고 조회
    bal_url = "/api/mix/v1/account/accounts"
    bal_res = requests.get("https://api.bitget.com" + bal_url, headers={**headers, 'ACCESS-SIGN': sign('GET', bal_url, '', ts)}).json()
    usdt_balance = next((x for x in bal_res.get('data', []) if x['marginCoin'] == 'USDT'), {}).get('available', '0')
    price = float(requests.get(f"https://api.bitget.com/api/mix/v1/market/ticker?symbol={data['symbol']}").json()['data']['lastPrice'])
    qty = float(usdt_balance) * data['bal_pct'] / 100 / price

    # 4. 주문
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
        body['triggerType'] = "market_price"
        body['callbackRate'] = str(data['trailing_stop'])

    ts3 = str(int(datetime.now().timestamp() * 1000))
    headers3 = {**headers, 'ACCESS-SIGN': sign('POST', order_url, body, ts3), 'ACCESS-TIMESTAMP': ts3}
    return requests.post("https://api.bitget.com" + order_url, headers=headers3, json=body).json()

# === 웹훅 ===
@app.route('/order', methods=['POST'])
def webhook():
    data = request.json or {}
    message = data.get('message', '')
    parsed = parse_v37(message)
    if not parsed:
        return jsonify({"error": "Invalid V37 message"}), 400

    def run():
        result = place_bitget_futures(parsed)
        print(f"V37 Order [{parsed['account']}]:", result)

    threading.Thread(target=run).start()
    return jsonify({"status": "V37 주문 전송됨", "account": parsed['account'], "symbol": parsed['symbol']}), 200

if __name__ == '__main__':
    app.run()
