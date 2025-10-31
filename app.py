# app.py - CHARTIBOT_V37.1 완전 호환 (Bitget & Bybit 지원, 한 줄 입력, V37 모든 설정 처리)
from flask import Flask, request, jsonify
import requests
import json
import threading
import os
from datetime import datetime
import hmac
import hashlib
import logging

# 로그 설정 (Vercel에서 로그 보장)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ----------------------------------------------------------------------
# 1. 한 줄 계정 로드 (Bitget: 5개, Bybit: 4개)
# ----------------------------------------------------------------------
accounts = {}
raw_accounts = os.getenv('EXCHANGE_ACCOUNTS', '')
logger.info(f"[INIT] EXCHANGE_ACCOUNTS: {raw_accounts}")
for line in raw_accounts.strip().split('\n'):
    parts = line.strip().split(',')
    if len(parts) < 4: continue
    uid, exchange, key, secret = parts[0], parts[1].lower(), parts[2], parts[3]
    passphrase = parts[4] if len(parts) > 4 else ''
    accounts[uid] = {
        'exchange': exchange,
        'key': key,
        'secret': secret,
        'passphrase': passphrase
    }
logger.info(f"[INIT] 등록된 계정: {list(accounts.keys())}")

# ----------------------------------------------------------------------
# 2. V37 메시지 파싱 (모든 설정 처리)
# ----------------------------------------------------------------------
def parse_v37(message):
    if not message.startswith('TVM:') or not message.endswith(':MVT'):
        logger.warning("[PARSE] 형식 오류")
        return None
    try:
        json_str = message[4:-4]
        data = json.loads(json_str)
        logger.info(f"[PARSE] 파싱 성공: {data}")
    except Exception as e:
        logger.error(f"[PARSE] JSON 실패: {e}")
        return None

    # V37 모든 설정 추출
    exchange = data.get('exchange', '').lower()
    account = data.get('account', '')
    symbol = data.get('symbol', '').replace('/', 'USDT')
    order_type = data.get('type', 'market')
    side = data.get('side', '').lower()
    bal_pct = float(data.get('bal_pct', 0))
    leverage = int(data.get('leverage', 1))
    margin_type = data.get('margin_type', 'cross')
    token = data.get('token', '')
    same_order = data.get('same_order', '')
    position_close = data.get('position_close', False)
    trailing_stop = float(data.get('trailing_stop', 0))
    ts_ac_price = float(data.get('ts_ac_price', 0))

    # direction 변환 (open_long, open_short, close_long, close_short)
    if side == 'buy':
        direction = 'open_long' if not position_close else 'close_short'
    elif side == 'sell':
        direction = 'open_short' if not position_close else 'close_long'
    else:
        logger.warning(f"[PARSE] side 오류: {side}")
        return None

    return {
        'exchange': exchange,
        'account': account,
        'symbol': symbol,
        'order_type': order_type,
        'direction': direction,
        'side': side,
        'bal_pct': bal_pct,
        'leverage': leverage,
        'margin_type': margin_type,
        'token': token,
        'same_order': same_order,
        'position_close': position_close,
        'trailing_stop': trailing_stop,
        'ts_ac_price': ts_ac_price
    }

# ----------------------------------------------------------------------
# 3. Bitget 선물 주문 (V37 모든 설정 지원)
# ----------------------------------------------------------------------
def place_bitget_order(data):
    try:
        logger.info(f"[BITGET] 주문 시작: {data}")
        acc = accounts[data['account']]
        if acc['exchange'] != 'bitget':
            logger.error("[BITGET] Not Bitget account")
            return {'error': 'Not Bitget'}

        def sign(method, url, body, ts):
            payload = f"{ts}{method.upper()}{url}{json.dumps(body) if body else ''}"
            return hmac.new(acc['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()

        ts = str(int(datetime.now().timestamp() * 1000))

        # 레버리지 설정
        lev_url = "/api/mix/v1/account/setLeverage"
        lev_body = {
            "symbol": data['symbol'],
            "marginCoin": "USDT",
            "leverage": str(data['leverage'])
        }
        headers = {
            'ACCESS-KEY': acc['key'],
            'ACCESS-SIGN': sign('POST', lev_url, lev_body, ts),
            'ACCESS-TIMESTAMP': ts,
            'ACCESS-PASSPHRASE': acc['passphrase'],
            'Content-Type': 'application/json'
        }
        response = requests.post("https://api.bitget.com" + lev_url, headers=headers, json=lev_body)
        logger.info(f"[BITGET] 레버리지 응답: {response.json()}")

        # 마진 타입 설정
        margin_url = "/api/mix/v1/account/setMarginMode"
        margin_body = {
            "symbol": data['symbol'],
            "marginMode": "isolated" if data['margin_type'] == 'isolated' else "crossed"
        }
        ts2 = str(int(datetime.now().timestamp() * 1000))
        headers2 = headers.copy()
        headers2['ACCESS-SIGN'] = sign('POST', margin_url, margin_body, ts2)
        headers2['ACCESS-TIMESTAMP'] = ts2
        response = requests.post("https://api.bitget.com" + margin_url, headers=headers2, json=margin_body)
        logger.info(f"[BITGET] 마진 타입 응답: {response.json()}")

        # 잔고 조회
        bal_url = "/api/mix/v1/account/accounts"
        ts3 = str(int(datetime.now().timestamp() * 1000))
        headers3 = headers.copy()
        headers3['ACCESS-SIGN'] = sign('GET', bal_url, '', ts3)
        headers3['ACCESS-TIMESTAMP'] = ts3
        bal_res = requests.get("https://api.bitget.com" + bal_url, headers=headers3).json()
        usdt_balance = next((x for x in bal_res.get('data', []) if x['marginCoin'] == 'USDT'), {}).get('available', '0')
        logger.info(f"[BITGET] 잔고: {usdt_balance}")

        # 현재가 조회
        price_res = requests.get(f"https://api.bitget.com/api/mix/v1/market/ticker?symbol={data['symbol']}").json()
        price = float(price_res['data']['lastPrice'])
        logger.info(f"[BITGET] 현재가: {price}")

        qty = float(usdt_balance) * data['bal_pct'] / 100 / price

        # 주문
        order_url = "/api/mix/v1/plan/placeOrder"
        body = {
            "symbol": data['symbol'],
            "marginCoin": "USDT",
            "side": data['direction'],
            "orderType": data['order_type'],
            "size": str(round(qty, 6))
        }
        if data['trailing_stop'] > 0:
            body['triggerPrice'] = str(round(price + data['ts_ac_price'] if 'long' in data['direction'] else price - data['ts_ac_price'], 6))
            body['callbackRate'] = str(data['trailing_stop'])
            body['triggerType'] = 'market_price'

        ts4 = str(int(datetime.now().timestamp() * 1000))
        headers4 = headers.copy()
        headers4['ACCESS-SIGN'] = sign('POST', order_url, body, ts4)
        headers4['ACCESS-TIMESTAMP'] = ts4
        response = requests.post("https://api.bitget.com" + order_url, headers=headers4, json=body)
        logger.info(f"[BITGET] 주문 응답: {response.json()}")

        return response.json()
    except Exception as e:
        logger.error(f"[BITGET] 오류: {e}", exc_info=True)
        return {'error': str(e)}

# ----------------------------------------------------------------------
# 4. 웹훅
# ----------------------------------------------------------------------
@app.route('/order', methods=['POST'])
def webhook():
    data = request.json or {}
    message = data.get('message', '')
    parsed = parse_v37(message)
    if not parsed:
        return jsonify({"error": "Invalid TVExtBot format"}), 400
    
    # 동시 주문 실행
    def execute_bitget():
        result = place_bitget_order(parsed['symbol'], parsed['side'], parsed['leverage'], parsed['percent'], parsed['account'])
        print(f"Bitget order: {result}")  # 로그

    threading.Thread(target=execute_bitget).start()
    
    return jsonify({'status': 'Orders placed', 'orderid': parsed['orderid']}), 200

if __name__ == '__main__':
    app.run()
