# app.py – 최종 완벽 버전 (오타 제거 + 잔고 조회 스킵)
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
# 3. Bitget 주문 (잔고 조회 스킵 + 오타 제거)
# ----------------------------------------------------------------------
def bitget_order(data):
    try:
        logger.info(f"[BITGET] === 주문 시작 (잔고 조회 스킵) ===")
        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bitget': return {'error': 'Invalid account'}

        def sign(method, url, body, ts):
            pre = f"{ts}{method.upper()}{url}"
            pre += json.dumps(body) if body else ''
            return hmac.new(acc['secret'].encode(), pre.encode(), hashlib.sha256).hexdigest()

        ts = str(int(datetime.now().timestamp() * 1000))
        hdr = {
            'ACCESS-KEY': acc['key'],
            'ACCESS-SIGN': '',
            'ACCESS-TIMESTAMP': ts,
            'ACCESS-PASSPHRASE': acc['passphrase'],
            'Content-Type': 'application/json'
        }

        # 1. 레버리지 (오타 제거)
        try:
            lev_url = '/api/v2/mix/account/set-leverage'
            lev_body = {'symbol': data['symbol'], 'marginCoin': 'USDT', 'leverage': str(data['leverage']), 'productType': 'umcbl'}
            hdr['ACCESS-SIGN'] = sign('POST', lev_url, lev_body, ts)
            r = requests.post('https://api.bitget.com' + lev_url, headers=hdr, json=lev_body, timeout=15)
            logger.info(f"[BITGET] 레버리지: {r.text}")
        except Exception as e:
            logger.warning(f"[BITGET] 레버리지 실패 (무시): {e}")

        # 2. 현재가
        try:
            r = requests.get('https://api.bitget.com/api/v2/mix/market/ticker', params={'symbol': data['symbol']}, timeout=15)
            j = r.json()
            if j.get('code') != '00000': return {'error': 'ticker error'}
            price = float(j['data'][0]['lastPr'])
            logger.info(f"[BITGET] 현재가: {price}")
        except Exception as e:
            logger.error(f"[BITGET] 티커 실패: {e}")
            return {'error': 'ticker error'}

        # 3. 수량 (10 USDT 고정)
        qty = round(10 / price, 6)
        logger.info(f"[BITGET] 주문 수량: {qty}")

        # 4. 주문
        try:
            order_url = '/api/v2/mix/order/place-order'
            body = {
                'symbol': data['symbol'],
                'marginCoin': 'USDT',
                'side': data['direction'],
                'orderType': 'market',
                'size': str(qty),
                'clientOid': f'v37_{int(datetime.now().timestamp())}',
                'productType': 'umcbl'
            }
            t4 = str(int(datetime.now().timestamp() * 1000))
            hdr4 = {**hdr, 'ACCESS-SIGN': sign('POST', order_url, body, t4), 'ACCESS-TIMESTAMP': t4}
            r = requests.post('https://api.bitget.com' + order_url, headers=hdr4, json=body, timeout=15)
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
# 4. 웹훅
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
