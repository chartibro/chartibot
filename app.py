import logging
from flask import Flask, request, jsonify
import requests, json, os, hashlib, hmac
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# === 계정 로드 ===
accounts = {}
raw = os.getenv('EXCHANGE_ACCOUNTS', '')
for line in raw.strip().split('\n'):
    p = [x.strip() for x in line.split(',')]
    if len(p) < 5: continue
    uid, exch, key, secret, passphrase = p
    accounts[uid] = {'exchange': exch.lower(), 'key': key, 'secret': secret, 'passphrase': passphrase}

# === V38 메시지 파싱 (Pine Script v6) ===
def parse_v38(msg: str):
    if not msg.startswith('TVM:') or not msg.endswith(':MVT'):
        return None
    try:
        payload = json.loads(msg[4:-4])
    except json.JSONDecodeError:
        return None

    side_raw = payload.get('side', '').lower()
    symbol_raw = payload.get('symbol', '')

    # 방향 결정
    if 'close' in side_raw:
        direction = 'close_short' if 'buy' in side_raw else 'close_long'
    else:
        direction = 'open_long' if 'buy' in side_raw else 'open_short'

    # 심볼 정규화
    symbol = symbol_raw.replace('/', 'USDT').upper()  # BTC/USDT → BTCUSDT

    return {
        'exchange': payload.get('exchange', '').lower(),
        'account': payload.get('account', ''),
        'symbol': symbol,
        'direction': direction,
        'leverage': int(payload.get('leverage', 10)),
        'token': payload.get('token', '')
    }

# === Bitget 서명 (공식 방식) ===
def bitget_sign(ts, method, url, body, secret):
    body_str = json.dumps(body) if body else ''
    pre_hash = f"{ts}{method.upper()}{url}{body_str}"
    logger.info(f"[SIGN] pre_hash: {pre_hash}")
    return hmac.new(secret.encode('utf-8'), pre_hash.encode('utf-8'), hashlib.sha256).hexdigest()

# === Bitget 주문 (100 DOJI 고정) ===
def bitget_order(data):
    try:
        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bitget':
            return {'error': 'Invalid account'}

        symbol = data['symbol']        # 예: DOGEUSDT
        direction = data['direction']  # open_long, close_long 등
        leverage = data['leverage']

        # --- 1. 레버리지 설정 ---
        try:
            ts1 = str(int(datetime.now().timestamp() * 1000))
            lev_url = '/api/v2/mix/account/set-leverage'
            lev_body = {
                "symbol": symbol,
                "marginCoin": "USDT",
                "leverage": str(leverage),
                "productType": "UMCBL_USDT"
            }
            sign1 = bitget_sign(ts1, 'POST', lev_url, lev_body, acc['secret'])
            headers = {
                'ACCESS-KEY': acc['key'],
                'ACCESS-TIMESTAMP': ts1,
                'ACCESS-PASSPHRASE': acc['passphrase'],
                'ACCESS-SIGN': sign1,
                'Content-Type': 'application/json',
                'locale': 'en-US'
            }
            r = requests.post('https://api.bitget.com' + lev_url, headers=headers, json=lev_body, timeout=15)
            logger.info(f"[LEV] {r.text}")
        except Exception as e:
            logger.error(f"[LEV ERROR] {e}")

        # --- 2. 주문 (100 DOJI 고정) ---
        size = 100  # 100 DOJI = 100 계약 (DOGEUSDT: 1계약 = 1 DOGE)

        ts2 = str(int(datetime.now().timestamp() * 1000))
        order_url = '/api/v2/mix/order/place-order'
        client_oid = f"v38_{int(datetime.now().timestamp())}"
        order_body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "side": direction,
            "orderType": "market",
            "size": str(size),
            "clientOid": client_oid,
            "productType": "UMCBL_USDT"
        }
        sign2 = bitget_sign(ts2, 'POST', order_url, order_body, acc['secret'])
        headers2 = {
            'ACCESS-KEY': acc['key'],
            'ACCESS-TIMESTAMP': ts2,
            'ACCESS-PASSPHRASE': acc['passphrase'],
            'ACCESS-SIGN': sign2,
            'Content-Type': 'application/json',
            'locale': 'en-US'
        }
        r = requests.post('https://api.bitget.com' + order_url, headers=headers2, json=order_body, timeout=15)
        result = r.json()
        logger.info(f"[ORDER] {result}")
        return result

    except Exception as e:
        logger.error(f"[ERROR] {e}")
        return {'error': str(e)}

# === 웹훅 엔드포인트 ===
@app.route('/order', methods=['POST'])
def webhook():
    payload = request.get_json(silent=True) or {}
    msg = payload.get('message', '')

    if not msg:
        return jsonify({'error': 'No message'}), 400

    parsed = parse_v38(msg)
    if not parsed:
        return jsonify({'error': 'Invalid V38 message'}), 400
    if parsed['account'] not in accounts:
        return jsonify({'error': 'Unknown account'}), 400

    result = bitget_order(parsed)
    return jsonify({'status': 'ok', 'result': result}), 200

if __name__ == '__main__':
    app.run()
