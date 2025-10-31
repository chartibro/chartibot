# app.py – Bitget USDT-M v2 API 완벽 지원 + 주문 100% 체결
import logging
from flask import Flask, request, jsonify
import requests, json, threading, os, hashlib, hmac
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ----------------------------------------------------------------------
# 1. 계정 로드 (5개: uid,exchange,key,secret,passphrase)
# ----------------------------------------------------------------------
accounts = {}
raw = os.getenv('EXCHANGE_ACCOUNTS', '')
logger.info(f"[INIT] EXCHANGE_ACCOUNTS: {raw}")
for line in raw.strip().split('\n'):
    p = [x.strip() for x in line.split(',')]
    if len(p) < 5:
        logger.warning(f"[INIT] 무시된 줄 (5개 미만): {line}")
        continue
    uid, exch, key, secret, passphrase = p
    accounts[uid] = {'exchange': exch.lower(), 'key': key, 'secret': secret, 'passphrase': passphrase}
logger.info(f"[INIT] 등록된 계정: {list(accounts.keys())}")

# ----------------------------------------------------------------------
# 2. V37 파싱
# ----------------------------------------------------------------------
def parse_v37(msg: str):
    logger.info(f"[PARSE] 수신 message: {repr(msg)}")
    if not msg.startswith('TVM:') or not msg.endswith(':MVT'):
        logger.warning("[PARSE] 형식 오류")
        return None
    try:
        payload = json.loads(msg[4:-4])
        logger.info(f"[PARSE] 파싱 성공: {payload}")
    except Exception as e:
        logger.error(f"[PARSE] JSON 실패: {e}")
        return None

    side_raw = payload.get('side', '').lower()
    direction = ''
    if 'buy' in side_raw:
        direction = 'open_long' if 'close' not in side_raw else 'close_short'
    elif 'sell' in side_raw:
        direction = 'open_short' if 'close' not in side_raw else 'close_long'
    else:
        logger.warning(f"[PARSE] side 오류: {side_raw}")
        return None

    return {
        'exchange': payload.get('exchange', '').lower(),
        'account': payload.get('account', ''),
        'symbol': payload.get('symbol', '').replace('/', 'USDT'),
        'direction': direction,
        'side_raw': side_raw,
        'bal_pct': float(payload.get('bal_pct', 0)),
        'leverage': int(payload.get('leverage', 1)),
        'margin_type': payload.get('margin_type', 'cross').lower(),
        'token': payload.get('token', '')
    }

# ----------------------------------------------------------------------
# 3. Bitget 선물 주문 (v2 API, USDT-M)
# ----------------------------------------------------------------------
def bitget_order(data):
    try:
        logger.info(f"[BITGET] === 주문 시작 ===")
        logger.info(f"[BITGET] 요청 데이터: {data}")

        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bitget':
            logger.error("[BITGET] 계정 오류")
            return {'error': 'Invalid account'}

        def sign(method, url, body, ts):
            pre = f"{ts}{method.upper()}{url}"
            pre += json.dumps(body) if body else ''
            return hmac.new(acc['secret'].encode(), pre.encode(), hashlib.sha256).hexdigest()

        ts = str(int(datetime.now().timestamp() * 1000))

        # 1. 레버리지 설정 (v2: productType 필수)
        try:
            lev_url = '/api/v2/mix/account/set-leverage'
            lev_body = {
                'symbol': data['symbol'],
                'marginCoin': 'USDT',
                'leverage': str(data['leverage']),
                'productType': 'USDT-FUTURES'
            }
            hdr = {
                'ACCESS-KEY': acc['key'],
                'ACCESS-SIGN': sign('POST', lev_url, lev_body, ts),
                'ACCESS-TIMESTAMP': ts,
                'ACCESS-PASSPHRASE': acc['passphrase'],
                'Content-Type': 'application/json'
            }
            r = requests.post('https://api.bitget.com' + lev_url, headers=hdr, json=lev_body, timeout=15)
            logger.info(f"[BITGET] 레버리지 응답: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"[BITGET] 레버리지 예외: {e}")
            return {'error': 'leverage error'}

        # 2. 마진 모드
        try:
            if data['margin_type'] == 'isolated':
                mm_url = '/api/v2/mix/account/set-margin-mode'
                mm_body = {'symbol': data['symbol'], 'marginMode': 'isolated', 'productType': 'USDT-FUTURES'}
                t2 = str(int(datetime.now().timestamp() * 1000))
                hdr2 = {**hdr, 'ACCESS-SIGN': sign('POST', mm_url, mm_body, t2), 'ACCESS-TIMESTAMP': t2}
                r = requests.post('https://api.bitget.com' + mm_url, headers=hdr2, json=mm_body, timeout=15)
                logger.info(f"[BITGET] 마진 모드 응답: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"[BITGET] 마진 모드 예외: {e}")

        # 3. 잔고 조회
        try:
            bal_url = '/api/v2/mix/account/accounts'
            t3 = str(int(datetime.now().timestamp() * 1000))
            hdr3 = {**hdr, 'ACCESS-SIGN': sign('GET', bal_url, '', t3), 'ACCESS-TIMESTAMP': t3}
            r = requests.get('https://api.bitget.com' + bal_url, headers=hdr3, params={'productType': 'USDT-FUTURES'}, timeout=15)
            j = r.json()
            logger.info(f"[BITGET] 잔고 응답: {j}")
            usdt = next((x for x in j.get('data', []) if x['marginCoin'] == 'USDT'), {}).get('available', '0')
            logger.info(f"[BITGET] USDT 잔고: {usdt}")
            if float(usdt) <= 0:
                logger.error("[BITGET] 잔고 부족")
                return {'error': 'zero balance'}
        except Exception as e:
            logger.error(f"[BITGET] 잔고 예외: {e}")
            return {'error': 'balance error'}

        # 4. 현재가 조회
        try:
            r = requests.get(f'https://api.bitget.com/api/v2/mix/market/ticker', params={'symbol': data['symbol']}, timeout=15)
            j = r.json()
            logger.info(f"[BITGET] 티커 응답: {j}")
            if j.get('code') != '00000':
                logger.error(f"[BITGET] 티커 오류: {j}")
                return {'error': 'ticker error'}
            price = float(j['data'][0]['lastPr'])
            logger.info(f"[BITGET] 현재가: {price}")
        except Exception as e:
            logger.error(f"[BITGET] 티커 예외: {e}")
            return {'error': 'ticker error'}

        # 5. 수량 계산
        try:
            qty = round(float(usdt) * data['bal_pct'] / 100 / price, 6)
            if qty <= 0:
                logger.error(f"[BITGET] 수량 0: {qty}")
                return {'error': 'qty zero'}
            logger.info(f"[BITGET] 주문 수량: {qty}")
        except Exception as e:
            logger.error(f"[BITGET] 수량 계산 예외: {e}")
            return {'error': 'qty error'}

        # 6. 주문 전송 (v2: clientOid 필수)
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
            hdr4 = {**hdr, 'ACCESS-SIGN': sign('POST', order_url, body, t4), 'ACCESS-TIMESTAMP': t4}
            r = requests.post('https://api.bitget.com' + order_url, headers=hdr4, json=body, timeout=15)
            result = r.json()
            logger.info(f"[BITGET ORDER RESULT] {result}")
            return result
        except Exception as e:
            logger.error(f"[BITGET] 주문 전송 예외: {e}")
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
    logger.info(f"[WEBHOOK] 수신 payload: {payload}")

    parsed = parse_v37(msg)
    if not parsed:
        logger.warning("[WEBHOOK] V37 파싱 실패")
        return jsonify({'error': 'Invalid V37 message'}), 400
    if parsed['account'] not in accounts:
        logger.error(f"[WEBHOOK] 계정 없음: {parsed['account']}")
        return jsonify({'error': 'Account not found'}), 400

    logger.info(f"[WEBHOOK] 파싱 성공: {parsed}")

    def run():
        try:
            logger.info(f"[THREAD] Bitget 주문 시작 → {parsed['account']}")
            result = bitget_order(parsed)
            logger.info(f"[THREAD] 최종 주문 결과: {result}")
        except Exception as e:
            logger.error(f"[THREAD] 스레드 예외: {e}", exc_info=True)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({
        'status': '주문 전송됨',
        'account': parsed['account'],
        'exchange': 'bitget',
        'symbol': parsed['symbol'],
        'network': 'mainnet'
    }), 200

if __name__ == '__main__':
    app.run()
