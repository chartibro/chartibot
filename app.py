# app.py - 다계정 지원 버전
from flask import Flask, request, jsonify
import requests
import json
import threading
import os
from datetime import datetime
import hmac
import hashlib

app = Flask(__name__)

# === 계정 로드 (환경 변수에서 자동 읽기) ===
accounts = {}
for i in range(1, 11):  # 최대 10개 계정
    uid = os.getenv(f'BITGET_UID_{i}')
    if not uid: break
    accounts[uid] = {
        'key': os.getenv(f'BITGET_KEY_{i}'),
        'secret': os.getenv(f'BITGET_SECRET_{i}'),
        'passphrase': os.getenv(f'BITGET_PASS_{i}')
    }

# Bybit도 나중에 추가 가능
bybit_key = os.getenv('BYBIT_API_KEY')
bybit_secret = os.getenv('BYBIT_SECRET')

# === 메시지 파싱 + 계정 선택 ===
def parse_tvext(message):
    if not message.startswith('TVM:') or not message.endswith(':MVT'):
        return None
    try:
        json_str = message[4:-4]
        data = json.loads(json_str)
        orderid = data.get('orderid')
        memo = data.get('memo', '')
        token = data.get('token', '').replace('/', '')
        account = data.get('account', '')  # TVM에 account 필드 추가!

        side = 'buy' if '매수' in memo else 'sell'
        percent = 0.1
        if '%' in memo:
            try: percent = float(memo.split('%')[0].split()[-1]) / 100
            except: pass
        return {'orderid': orderid, 'symbol': token, 'side': side, 'percent': percent, 'account': account}
    except:
        return None

# === Bitget 주문 (계정별) ===
def place_bitget_order(symbol, side, percent, account_uid):
    if account_uid not in accounts:
        return {"error": f"Account {account_uid} not found"}
    acc = accounts[account_uid]
    
    # 서명
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
        return jsonify({"error": "Missing account in TVM"}), 400

    def run_bitget():
        result = place_bitget_order(parsed['symbol'], parsed['side'], parsed['percent'], parsed['account'])
        print(f"Bitget [{parsed['account']}]:", result)

    threading.Thread(target=run_bitget).start()
    return jsonify({"status": "주문 전송됨", "account": parsed['account']}), 200
