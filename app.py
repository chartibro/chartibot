# app.py - TVExtBot 스타일 한 줄 입력 버전 
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

# === 메시지 파싱 ===
def parse_tvext(message):
    if not message.startswith('TVM:') or not message.endswith(':MVT'):
        return None
    try:
        json_str = message[4:-4]
        data = json.loads(json_str)
        orderid = data.get('orderid')
        memo = data.get('memo', '')
        token = data.get('token', '').replace('/', '')
        account = data.get('account', '')

        side = 'buy' if '매수' in memo else 'sell'
        percent = 0.1
        if '%' in memo:
            try: percent = float(memo.split('%')[0].split()[-1]) / 100
            except: pass
        return {'orderid': orderid, 'symbol': token, 'side': side, 'percent': percent, 'account': account}
    except:
        return None

# === Bitget 주문 ===
def place_bitget_order(symbol, side, percent, account_uid):
    if account_uid not in accounts:
        return {"error": f"Account {account_uid} not found"}
    acc = accounts[account_uid]
    
    def sign(method, url, body, ts):
        payload = f"{ts}{method.upper()}{url}{json.dumps(body) if body else ''}"
        return hmac.new(acc['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()

    # 잔고
    ts = str(int(datetime.now().timestamp() * 1000))
    bal_url = "/api/spot/v1/account/assets"
    headers = {
        'ACCESS-KEY': acc['key'],
        'ACCESS-SIGN': sign('GET', bal_url, '', ts),
        'ACCESS-TIMESTAMP': ts,
        'ACCESS-PASSPHRASE': acc['passphrase'],
        'Content-Type': 'application/json'
    }
    bal_res = requests.get("https://api.bitget.com" + bal_url, headers=headers).json()
    usdt = next((x for x in bal_res.get('data', []) if x['coin'] == 'USDT'), {}).get('available', '0')
    price = float(requests.get(f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={symbol}").json()['data']['last'])
    qty = float(usdt) * percent / price

    # 주문
    body = {"symbol": symbol, "side": side, "orderType": "market", "force": "normal", "size": str(round(qty, 6))}
    ts2 = str(int(datetime.now().timestamp() * 1000))
    headers2 = headers.copy()
    headers2['ACCESS-SIGN'] = sign('POST', '/api/spot/v1/trade/place-order', body, ts2)
    headers2['ACCESS-TIMESTAMP'] = ts2
    return requests.post("https://api.bitget.com/api/spot/v1/trade/place-order", headers=headers2, json=body).json()

# === 웹훅 ===
@app.route('/order', methods=['POST'])
def webhook():
    data = request.json or {}
    message = data.get('message', '')
    parsed = parse_tvext(message)
    if not parsed or not parsed['account']:
        return jsonify({"error": "Missing account"}), 400

    def run():
        result = place_bitget_order(parsed['symbol'], parsed['side'], parsed['percent'], parsed['account'])
        print(f"Bitget [{parsed['account']}]:", result)

    threading.Thread(target=run).start()
    return jsonify({"status": "주문 전송됨", "account": parsed['account']}), 200

if __name__ == '__main__':
    app.run()
