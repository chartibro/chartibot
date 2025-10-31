import logging
from flask import Flask, request, jsonify
import requests, json, os, hashlib, hmac
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 계정 로드
accounts = {}
raw = os.getenv('EXCHANGE_ACCOUNTS', '')
for line in raw.strip().split('\n'):
    p = [x.strip() for x in line.split(',')]
    if len(p) < 5: continue
    uid, exch, key, secret, passphrase = p
    accounts[uid] = {'exchange': exch.lower(), 'key': key, 'secret': secret, 'passphrase': passphrase}

# V37 파싱
def parse_v37(msg: str):
    if not msg.startswith('TVM:') or not msg.endswith(':MVT'): return None
    try: payload = json.loads(msg[4:-4])
    except: return None
    side_raw = payload.get('side', '').lower()
    direction = 'open_long' if 'buy' in side_raw else 'open_short'
    if 'close' in side_raw:
        direction = 'close_short' if 'buy' in side_raw else 'close_long'
    symbol = payload.get('symbol', '').replace('/', 'USDT')
    return {
        'exchange': payload.get('exchange', '').lower(),
        'account': payload.get('account', ''),
        'symbol': symbol,
        'direction': direction,
        'bal_pct': float(payload.get('bal_pct', 0)),
        'leverage': int(payload.get('leverage', 1)),
        'margin_type': payload.get('margin_type', 'cross').lower(),
        'token': payload.get('token', '')
    }

# 서명 (공식 예제 그대로)
def bitget_sign(ts, method, url, body_dict, secret):
    body_str = json.dumps(body_dict, separators=(',', ':'), sort_keys=True) if body_dict else ''
    pre_hash = f"{ts}{method.upper()}{url}{body_str}"
    logger.info(f"[SIGN] pre_hash: {pre_hash}")
    return hmac.new(secret.encode(), pre_hash.encode(), hashlib.sha256).hexdigest()

# 주문
def bitget_order(data):
    try:
        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bitget': return {'error': 'Invalid account'}

        # 1. 레버리지
        try:
            ts1 = str(int(datetime.now().timestamp() * 1000))
            lev_url = '/api/v2/mix/account/set-leverage'
            lev_body = {"symbol": data['symbol'], "marginCoin": "USDT", "leverage": str(data['leverage']), "productType": "umcbl"}
            sign1 = bitget_sign(ts1, 'POST', lev_url, lev_body, acc['secret'])
            headers = {
                'ACCESS-KEY': acc['key'],
                'ACCESS-TIMESTAMP': ts1,
                'ACCESS-PASSPHRASE': acc['passphrase'],
                'ACCESS-SIGN': sign1,
                'Content-Type': 'application/json'
            }
            r = requests.post('https://api.bitget.com' + lev_url, headers=headers, json=lev_body, timeout=15)
            logger.info(f"[LEV] {r.text}")
        except: pass

        # 2. 주문
        ts2 = str(int(datetime.now().timestamp() * 1000))
        order_url = '/api/v2/mix/order/place-order'
        qty = round(10 / 0.083, 6)  # 10 USDT 고정
        client_oid = f"v37_{int(datetime.now().timestamp())}"
        order_body = {
            "symbol": data['symbol'],
            "marginCoin": "USDT",
            "side": data['direction'],
            "orderType": "market",
            "size": str(qty),
            "clientOid": client_oid,
            "productType": "umcbl"
        }
        sign2 = bitget_sign(ts2, 'POST', order_url, order_body, acc['secret'])
        headers2 = {
            'ACCESS-KEY': acc['key'],
            'ACCESS-TIMESTAMP': ts2,
            'ACCESS-PASSPHRASE': acc['passphrase'],
            'ACCESS-SIGN': sign2,
            'Content-Type': 'application/json'
        }
        r = requests.post('https://api.bitget.com' + order_url, headers=headers2, json=order_body, timeout=15)
        result = r.json()
        logger.info(f"[ORDER] {result}")
        return result

    except Exception as e:
        logger.error(f"[ERROR] {e}")
        return {'error': str(e)}

# 웹훅
@app.route('/order', methods=['POST'])
def webhook():
    payload = request.get_json(silent=True) or {}
    msg = payload.get('message', '')
    parsed = parse_v37(msg)
    if not parsed or parsed['account'] not in accounts:
        return jsonify({'error': 'Invalid'}), 400
    result = bitget_order(parsed)
    return jsonify({'status': 'ok', 'result': result}), 200

if __name__ == '__main__':
    app.run()
