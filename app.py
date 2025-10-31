# app.py – Bitget v2 API 문서 기반 완벽 버전
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
    logger.info(f"[PARSE] 메시지: {repr(msg)}")
    if not msg.startswith('TVM:') or not msg.endswith(':MVT'): return None
    try: payload = json.loads(msg[4:-4])
    except Exception as e:
        logger.error(f"[PARSE] JSON 실패: {e}")
        return None

    side_raw = payload.get('side', '').lower()
    direction = 'open_long' if 'buy' in side_raw else 'open_short'
    if 'close' in side_raw:
        direction = 'close_short' if 'buy' in side_raw else 'close_long'

    symbol = payload.get('symbol', '').replace('/', 'USDT')

    logger.info(f"[PARSE] 파싱 성공: {payload}")
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
# 3. Bitget 서명 함수 (문서 기반)
# ----------------------------------------------------------------------
def bitget_sign(method, url, body, secret, ts):
    body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False) if body else ''
    pre_hash = f"{ts}{method.upper()}{url}{body_str}"
    return hmac.new(secret.encode(), pre_hash.encode(), hashlib.sha256).hexdigest()

# ----------------------------------------------------------------------
# 4. Bitget 주문 (v2 API, 문서 기반 수정)
# ----------------------------------------------------------------------
def bitget_order(data):
    try:
        logger.info(f"[BITGET] 주문 시작: {data}")
        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bitget': return {'error': 'Invalid account'}

        ts = str(int(datetime.now().timestamp() * 1000))
        hdr_base = {
            'ACCESS-KEY': acc['key'],
            'ACCESS-TIMESTAMP': ts,
            'ACCESS-PASSPHRASE': acc['passphrase'],
            'Content-Type': 'application/json'
        }

        # 1. 레버리지 (productType: USDT-FUTURES)
        try:
            lev_url = '/api/v2/mix/account/set-leverage'
            lev_body = {
                'symbol': data['symbol'],
                'marginCoin': 'USDT',
                'leverage': str(data['leverage']),
                'productType': 'USDT-FUTURES'
            }
            hdr = {**hdr_base, 'ACCESS-SIGN': bitget_sign('POST', lev_url, lev_body, acc['secret'], ts)}
            r = requests.post('https://api.bitget.com' + lev_url, headers=hdr, json=lev_body, timeout=15)
            logger.info(f"[BITGET] 레버리지 응답: {r.status_code} {r.text}")
        except Exception as e:
            logger.warning(f"[BITGET] 레버리지 실패 (무시): {e}")

        # 2. 마진 모드
        try:
            if data['margin_type'] == 'isolated':
                mm_url = '/api/v2/mix/account/set-margin-mode'
                mm_body = {'symbol': data['symbol'], 'marginMode': 'isolated', 'productType': 'USDT-FUTURES'}
                t2 = str(int(datetime.now().timestamp() * 1000))
                hdr2 = {**hdr_base, 'ACCESS-SIGN': bitget_sign('POST', mm_url, mm_body, acc['secret'], t2), 'ACCESS-TIMESTAMP': t2}
                r = requests.post('https://api.bitget.com' + mm_url, headers=hdr2, json=mm_body, timeout=15)
                logger.info(f"[BITGET] 마진 모드 응답: {r.status_code} {r.text}")
        except Exception as e:
            logger.warning(f"[BITGET] 마진 모드 실패 (무시): {e}")

        # 3. 잔고 조회 (accounts)
        try:
            bal_url = '/api/v2/mix/account/accounts'
            t3 = str(int(datetime.now().timestamp() * 1000))
            hdr3 = {**hdr_base, 'ACCESS-SIGN': bitget_sign('GET', bal_url, {}, acc['secret'], t3), 'ACCESS-TIMESTAMP': t3}
            r = requests.get('https://api.bitget.com' + bal_url, headers=hdr3, params={'productType': 'USDT-FUTURES'}, timeout=15)
            j = r.json()
            logger.info(f"[BITGET] 잔고 응답: {j}")
            if j.get('code') != '00000':
                logger.error(f"[BITGET] 잔고 API 실패: {j}")
                return {'error': 'balance api failed'}
            if not j.get('data'):
                logger.error("[BITGET] data 빈 배열")
                return {'error': 'no data in balance'}
            usdt_data = next((x for x in j['data'] if x.get('marginCoin') == 'USDT'), None)
            if not usdt_data:
                logger.error("[BITGET] USDT 없음")
                return {'error': 'no usdt'}
            balance = usdt_data.get('available') or usdt_data.get('availableAmt') or '0'
            logger.info(f"[BITGET] USDT 잔고: {balance}")
            if float(balance) <= 0:
                return {'error': 'zero balance'}
        except Exception as e:
            logger.error(f"[BITGET] 잔고 예외: {e}")
            return {'error': 'balance exception'}

        # 4. 현재가
        try:
            r = requests.get('https://api.bitget.com/api/v2/mix/market/ticker', params={'symbol': data['symbol']}, timeout=15)
            j = r.json()
            logger.info(f"[BITGET] 티커 응답: {j}")
            if j.get('code') != '00000' or not j.get('data'):
                logger.error(f"[BITGET] 티커 API 실패: {j}")
                return {'error': 'ticker api failed'}
            price_data = j['data'][0]
            price = float(price_data.get('last') or price_data.get('lastPr') or '0')
            if price == 0:
                logger.error(f"[BITGET] 가격 0: {price_data}")
                return {'error': 'price zero'}
            logger.info(f"[BITGET] 현재가: {price}")
        except Exception as e:
            logger.error(f"[BITGET] 티커 예외: {e}")
            return {'error': 'ticker exception'}

        # 5. 수량
        qty = round(float(balance) * data['bal_pct'] / 100 / price, 6)
        if qty <= 0: return {'error': 'qty zero'}
        logger.info(f"[BITGET] 주문 수량: {qty}")

        # 6. 주문
        try:
            order_url = '/api/v2/mix/order/place-order'
            body = {
                'symbol': data['symbol'],
                'marginCoin': 'USDT',
                'side': data['direction'],
                'orderType': 'market',
                'size': str(qty),
                'clientOid': f'v37_{int(datetime.now().timestamp())}',
                'productType': 'USDT-FUTURES'
            }
            t4 = str(int(datetime.now().timestamp() * 1000))
            hdr4 = {**hdr_base, 'ACCESS-SIGN': bitget_sign('POST', order_url, body, acc['secret'], t4), 'ACCESS-TIMESTAMP': t4}
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
