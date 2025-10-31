# app.py – sort_keys=True + 새 timestamp + 고정 10 USDT
import logging
from flask import Flask, request, jsonify
import requests, json, os, hashlib, hmac
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ----------------------------------------------------------------------
# 1. 계정 로드
# ----------------------------------------------------------------------
accounts = {}
raw = os.getenv('EXCHANGE_ACCOUNTS', '')
logger.info(f"[INIT] EXCHANGE_ACCOUNTS: {raw}")
for line in raw.strip().split('\n'):
    p = [x.strip() for x in line.split(',')]
    if len(p) < 5: continue
    uid, exch, key, secret, passphrase = p
    accounts[uid] = {'exchange': exch.lower(), 'key': key, 'secret': secret, 'passphrase': passphrase}
logger.info(f"[INIT] 등록된 계정: {list(accounts.keys())}")

# ----------------------------------------------------------------------
# 2. V37 파싱
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# 3. Bitget 서명 함수 (sort_keys=True 필수)
# ----------------------------------------------------------------------
def bitget_sign(method, url, body_dict, secret, ts):
    body_str = json.dumps(body_dict, separators=(',', ':'), sort_keys=True, ensure_ascii=False) if body_dict else ''
    pre_hash = f"{ts}{method.upper()}{url}{body_str}"
    logger.info(f"[SIGN] pre_hash: {pre_hash}")
    return hmac.new(secret.encode(), pre_hash.encode(), hashlib.sha256).hexdigest()

# ----------------------------------------------------------------------
# 4. Bitget 주문 (새로운 timestamp + 고정 10 USDT)
# ----------------------------------------------------------------------
def bitget_order(data):
    try:
        logger.info(f"[BITGET] === 주문 시작 ===")
        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bitget': return {'error': 'Invalid account'}

        # 1. 레버리지
        try:
            ts1 = str(int(datetime.now().timestamp() * 1000))
            lev_url = '/api/v2/mix/account/set-leverage'
            lev_body = {
                'symbol': data['symbol'],
                'marginCoin': 'USDT',
                'leverage': str(data['leverage']),
                'productType': 'umcbl'
            }
            sign1 = bitget_sign('POST', lev_url, lev_body, acc['secret'], ts1)
            hdr1 = {
                'ACCESS-KEY': acc['key'],
                'ACCESS-TIMESTAMP': ts1,
                'ACCESS-PASSPHRASE': acc['passphrase'],
                'Content-Type': 'application/json',
                'ACCESS-SIGN': sign1
            }
            r = requests.post('https://api.bitget.com' + lev_url, headers=hdr1, json=lev_body, timeout=15)
            logger.info(f"[BITGET] 레버리지 응답: {r.text}")
        except Exception as e:
            logger.warning(f"[BITGET] 레버리지 실패 (무시): {e}")

        # 2. 고정 가격 + 수량
        price = 0.083
        qty = round(10 / price, 6)
        logger.info(f"[BITGET] 고정 가격: {price}, 수량: {qty}")

        # 3. 주문
        try:
            ts2 = str(int(datetime.now().timestamp() * 1000))
            order_url = '/api/v2/mix/order/place-order'
            order_body = {
                'symbol': data['symbol'],
                'marginCoin': 'USDT',
                'side': data['direction'],
                'orderType': 'market',
                'size': str(qty),
                'clientOid': f'v37_{int(datetime.now().timestamp())}',
                'productType': 'umcbl'
            }
            sign2 = bitget_sign('POST', order_url, order_body, acc['secret'], ts2)
            hdr2 = {
                'ACCESS-KEY': acc['key'],
                'ACCESS-TIMESTAMP': ts2,
                'ACCESS-PASSPHRASE': acc['passphrase'],
                'Content-Type': 'application/json',
                'ACCESS-SIGN': sign2
            }
            r = requests.post('https://api.bitget.com' + order_url, headers=hdr2, json=order_body, timeout=15)
            result = r.json()
            logger.info(f"[BITGET ORDER RESULT] {result}")
            return result
        except Exception as e:
            logger.error(f"[BITGET] 주문 예외: {e}")
            return {'error': 'order error'}

    except Exception as e:
        logger.error(f"[BITGET] 치명적 오류: {e}", exc_info=True)
        return {'error': 'fatal'}

# ----------------------------------------------------------------------
# 5. 웹훅
# ----------------------------------------------------------------------
@app.route('/order', methods=['POST'])
def webhook():
    payload = request.get_json(silent=True) or {}
    msg = payload.get('message', '')
    logger.info(f"[WEBHOOK] 수신: {payload}")

    parsed = parse_v37(msg)
    if not parsed or parsed['account'] not in accounts:
        return jsonify({'error': 'Invalid'}), 400

    logger.info(f"[WEBHOOK] 파싱 성공: {parsed}")
    result = bitget_order(parsed)
    logger.info(f"[SYNC] 최종 결과: {result}")

    return jsonify({
        'status': '주문 완료',
        'account': parsed['account'],
        'result': result
    }), 200

if __name__ == '__main__':
    app.run()
