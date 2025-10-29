# app.py
from flask import Flask, request, jsonify
import requests
import json
import threading
import os
from datetime import datetime
import hmac
import hashlib

app = Flask(__name__)

# === 환경 변수에서 API 키 가져오기 (나중에 Vercel에서 설정) ===
BITGET_API_KEY = os.getenv('BITGET_API_KEY')
BITGET_SECRET = os.getenv('BITGET_SECRET')
BITGET_PASSPHRASE = os.getenv('BITGET_PASSPHRASE')

BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_SECRET = os.getenv('BYBIT_SECRET')

# === TVExtBot 메시지 파싱 ===
def parse_tvext(message):
    if not message.startswith('TVM:') or not message.endswith(':MVT'):
        return None
    try:
        json_str = message[4:-4]
        data = json.loads(json_str)
        orderid = data.get('orderid')
        memo = data.get('memo', '')
        symbol = data.get('token', '').replace('/', '')  # BTC/USDT → BTCUSDT

        # 간단히 "매수 30%" → buy, 0.3
        side = 'buy' if '매수' in memo else 'sell'
        percent = 0.1  # 기본 10%
        if '%' in memo:
            try: percent = float(memo.split('%')[0].split()[-1]) / 100
            except: pass
        return {'orderid': orderid, 'symbol': symbol, 'side': side, 'percent': percent}
    except:
        return None

# === Bitget 서명 + 주문 ===
def bitget_sign(method, url, body, timestamp):
    payload = f"{timestamp}{method.upper()}{url}{json.dumps(body) if body else ''}"
    signature = hmac.new(BITGET_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return signature

def place_bitget_order(symbol, side, percent):
    # 1. 잔고 조회 (USDT 기준)
    balance_url = "/api/spot/v1/account/assets"
    ts = str(int(datetime.now().timestamp() * 1000))
    sign = bitget_sign('GET', balance_url, '', ts)
    headers = {
        'ACCESS-KEY': BITGET_API_KEY,
        'ACCESS-SIGN': sign,
        'ACCESS-TIMESTAMP': ts,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
        'Content-Type': 'application/json'
    }
    bal_res = requests.get("https://api.bitget.com" + balance_url, headers=headers).json()
    usdt_balance = next((x for x in bal_res.get('data', []) if x['coin'] == 'USDT'), {}).get('available', '0')
    price_res = requests.get(f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={symbol}").json()
    price = float(price_res['data']['last']) if price_res.get('data') else 1
    qty = float(usdt_balance) * percent / price

    # 2. 주문
    order_url = "/api/spot/v1/trade/place-order"
    body = {
        "symbol": symbol,
        "side": side,
        "orderType": "market",
        "force": "normal",
        "size": str(round(qty, 6))
    }
    ts2 = str(int(datetime.now().timestamp() * 1000))
    sign2 = bitget_sign('POST', order_url, body, ts2)
    headers2 = headers.copy()
    headers2['ACCESS-SIGN'] = sign2
    headers2['ACCESS-TIMESTAMP'] = ts2
    return requests.post("https://api.bitget.com" + order_url, headers=headers2, json=body).json()

# === Bybit 서명 + 주문 ===
def bybit_sign(params, timestamp):
    pre_hash = f"{BYBIT_API_KEY}{timestamp}5000{json.dumps(params)}"
    return hmac.new(BYBIT_SECRET.encode(), pre_hash.encode(), hashlib.sha256).hexdigest()

def place_bybit_order(symbol, side, percent):
    # 잔고
    ts = str(int(datetime.now().timestamp() * 1000))
    bal_params = {"category": "spot", "coin": "USDT"}
    sign = bybit_sign(bal_params, ts)
    bal_res = requests.get(
        "https://api.bybit.com/v5/account/wallet-balance",
        headers={'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-SIGN': sign, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-RECV-WINDOW': '5000'},
        params=bal_params
    ).json()
    usdt_balance = next((x for x in bal_res.get('result', {}).get('balances', []) if x['coin'] == 'USDT'), {}).get('walletBalance', '0')
    price_res = requests.get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}").json()
    price = float(price_res['result']['list'][0]['lastPrice']) if price_res.get('result', {}).get('list') else 1
    qty = float(usdt_balance) * percent / price

    # 주문
    order_params = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy" if side == 'buy' else "Sell",
        "orderType": "Market",
        "qty": str(round(qty, 6))
    }
    ts2 = str(int(datetime.now().timestamp() * 1000))
    sign2 = bybit_sign(order_params, ts2)
    return requests.post(
        "https://api.bybit.com/v5/order/create",
        headers={'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-SIGN': sign2, 'X-BAPI-TIMESTAMP': ts2, 'X-BAPI-RECV-WINDOW': '5000'},
        json=order_params
    ).json()

# === 웹훅 엔드포인트 ===
@app.route('/order', methods=['POST'])
def webhook():
    data = request.json or {}
    message = data.get('message', '')
    parsed = parse_tvext(message)
    if not parsed:
        return jsonify({"error": "Invalid TVExtBot format"}), 400

    def run_bitget():
        try: result = place_bitget_order(parsed['symbol'], parsed['side'], parsed['percent'])
        except Exception as e: result = {"error": str(e)}
        print("Bitget:", result)

    def run_bybit():
        try: result = place_bybit_order(parsed['symbol'], parsed['side'], parsed['percent'])
        except Exception as e: result = {"error": str(e)}
        print("Bybit:", result)

    threading.Thread(target=run_bitget).start()
    threading.Thread(target=run_bybit).start()

    return jsonify({"status": "주문 전송됨", "orderid": parsed['orderid']}), 200

if __name__ == '__main__':
    app.run()
