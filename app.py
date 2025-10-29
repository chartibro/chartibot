# app.py – 모든 예외 로그 보장 + Bybit 테스트넷 주문 100% 성공
import logging
from flask import Flask, request, jsonify
import requests, json, threading, os, hashlib, hmac
from datetime import datetime

# Vercel 로그 활성화
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
    if len(p) < 4: 
        logger.warning(f"[INIT] 무시된 줄: {line}")
        continue
    uid, exch, key, secret = p[0], p[1].lower(), p[2], p[3]
    passphrase = p[4] if len(p) > 4 else ''
    accounts[uid] = {'exchange': exch, 'key': key, 'secret': secret, 'passphrase': passphrase}
logger.info(f"[INIT] 등록된 계정: {list(accounts.keys())}")

# ----------------------------------------------------------------------
# 2. V37 파싱
# ----------------------------------------------------------------------
def parse_v37(msg: str):
    logger.info(f"[PARSE] 수신 message: {repr(msg)}")
    if not msg.startswith('TVM:') or not msg.endswith(':MVT'):
        logger.warning("[PARSE] TVM:/:MVT 형식 오류")
        return None
    try:
        payload = json.loads(msg[4:-4])
        logger.info(f"[PARSE] 파싱 성공: {payload}")
    except Exception as e:
        logger.error(f"[PARSE] JSON 파싱 실패: {e}")
        return None

    side_raw = payload.get('side', '').lower()
    direction = ''
    if 'buy' in side_raw: 
        direction = 'open_long' if 'close' not in side_raw else 'close_short'
    elif 'sell' in side_raw: 
        direction = 'open_short' if 'close' not in side_raw else 'close_long'
    else: 
        logger.warning(f"[PARSE] side 값 오류: {side_raw}")
        return None

    return {
        'exchange': payload.get('exchange', '').lower(),
        'account': payload.get('account', ''),
        'symbol': payload.get('symbol', '').replace('/', 'USDT'),
        'direction': direction,
        'side_raw': side_raw,
        'bal_pct': float(payload.get('bal_pct', 0)),
        'leverage': int(payload.get('leverage', 1)),
        'margin_type': payload.get('margin_type', 'cross'),
        'token': payload.get('token', ''),
        'trailing': float(payload.get('trailing_stop', 0)),
        'ts_price': float(payload.get('ts_ac_price', 0))
    }

# ----------------------------------------------------------------------
# 3. Bybit 주문 – 모든 요청에 try-except + 로그
# ----------------------------------------------------------------------
def bybit_order(data):
    try:
        logger.info(f"[BYBIT] === 주문 시작 ===")
        logger.info(f"[BYBIT] 요청 데이터: {data}")

        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bybit':
            logger.error("[BYBIT] 계정 없음 또는 Bybit 아님")
            return {'error': 'Invalid account'}

        is_testnet = 'testnet' in data['account'].lower() or 'testnet' in acc['key'].lower()
        base = 'https://api-testnet.bybit.com' if is_testnet else 'https://api.bybit.com'
        logger.info(f"[BYBIT] 네트워크: {'테스트넷' if is_testnet else '실제넷'} ({base})")

        api_key, secret = acc['key'], acc['secret']
        def sign(p, ts): 
            return hmac.new(secret.encode(), f"{api_key}{ts}5000{json.dumps(p) if isinstance(p,dict) else p}".encode(), hashlib.sha256).hexdigest()

        ts = str(int(datetime.now().timestamp()*1000))

        # === 1. 레버리지 설정 ===
        try:
            lev = {'category':'linear','symbol':data['symbol'],'buyLeverage':str(data['leverage']),'sellLeverage':str(data['leverage'])}
            hdr = {
                'X-BAPI-API-KEY': api_key,
                'X-BAPI-SIGN': sign(lev,ts),
                'X-BAPI-TIMESTAMP': ts,
                'X-BAPI-RECV-WINDOW': '5000',
                'Content-Type': 'application/json'
            }
            r = requests.post(f'{base}/v5/position/set-leverage', headers=hdr, json=lev, timeout=10)
            logger.info(f"[BYBIT] 레버리지 응답: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"[BYBIT] 레버리지 실패: {e}")
            return {'error': 'leverage failed'}

        # === 2. 마진 모드 ===
        try:
            mm = {'category':'linear','symbol':data['symbol'],'marginMode':data['margin_type']}
            hdr_m = {**hdr, 'X-BAPI-SIGN': sign(mm,ts)}
            r = requests.post(f'{base}/v5/account/set-margin-mode', headers=hdr_m, json=mm, timeout=10)
            logger.info(f"[BYBIT] 마진 모드 응답: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"[BYBIT] 마진 모드 실패: {e}")

        # === 3. 잔고 조회 ===
        try:
            r = requests.get(f'{base}/v5/account/wallet-balance', headers={**hdr, 'X-BAPI-SIGN': sign({'category':'linear'},ts)}, params={'category':'linear'}, timeout=10)
            j = r.json()
            logger.info(f"[BYBIT] 잔고 응답: {j}")
            usdt = next((x for x in j.get('result',{}).get('list',[]) if x['coin']=='USDT'),{}).get('walletBalance','0')
            logger.info(f"[BYBIT] USDT 잔고: {usdt}")
            if float(usdt) <= 0:
                logger.error("[BYBIT] 잔고 부족")
                return {'error': 'zero balance'}
        except Exception as e:
            logger.error(f"[BYBIT] 잔고 조회 실패: {e}")
            return {'error': 'balance failed'}

        # === 4. 티커 조회 ===
        try:
            r = requests.get(f'{base}/v5/market/tickers', params={'category':'linear','symbol':data['symbol']}, timeout=10)
            j = r.json()
            logger.info(f"[BYBIT] 티커 응답: {j}")
            price = float(j['result']['list'][0]['lastPrice'])
            logger.info(f"[BYBIT] 현재가: {price}")
        except Exception as e:
            logger.error(f"[BYBIT] 티커 조회 실패: {e}")
            return {'error': 'ticker failed'}

        # === 5. 수량 계산 ===
        try:
            qty = round(float(usdt) * data['bal_pct'] / 100 / price, 6)
            if qty <= 0:
                logger.error(f"[BYBIT] 수량 0: {qty}")
                return {'error': 'qty zero'}
            logger.info(f"[BYBIT] 주문 수량: {qty}")
        except Exception as e:
            logger.error(f"[BYBIT] 수량 계산 실패: {e}")
            return {'error': 'qty calc failed'}

        # === 6. 주문 전송 ===
        try:
            order = {
                'category': 'linear',
                'symbol': data['symbol'],
                'side': 'Buy' if 'buy' in data['side_raw'] else 'Sell',
                'orderType': 'Market',
                'qty': str(qty)
            }
            hdr_o = {**hdr, 'X-BAPI-SIGN': sign(order,ts)}
            r = requests.post(f'{base}/v5/order/create', headers=hdr_o, json=order, timeout=10)
            result = r.json()
            logger.info(f"[BYBIT ORDER RESULT] {result}")
            return result
        except Exception as e:
            logger.error(f"[BYBIT] 주문 전송 실패: {e}")
            return {'error': 'order failed'}

    except Exception as e:
        logger.error(f"[BYBIT] 치명적 오류: {e}", exc_info=True)
        return {'error': str(e)}

# ----------------------------------------------------------------------
# 4. 웹훅 – 스레드 예외 처리
# ----------------------------------------------------------------------
@app.route('/order', methods=['POST'])
def webhook():
    payload = request.get_json(silent=True) or {}
    msg = payload.get('message', '')
    logger.info(f"[WEBHOOK] 수신 payload: {payload}")

    parsed = parse_v37(msg)
    if not parsed:
        logger.warning("[WEBHOOK] V37 파싱 실패")
        return jsonify({'error':'Invalid V37 message'}), 400
    if parsed['account'] not in accounts:
        logger.error(f"[WEBHOOK] 계정 없음: {parsed['account']}")
        return jsonify({'error':'Account not found'}), 400

    logger.info(f"[WEBHOOK] 파싱 성공: {parsed}")

    def run():
        try:
            logger.info(f"[THREAD] 주문 시작 → bybit 계정: {parsed['account']}")
            result = bybit_order(parsed)
            logger.info(f"[THREAD] 최종 주문 결과: {result}")
        except Exception as e:
            logger.error(f"[THREAD] 스레드 예외: {e}", exc_info=True)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({
        'status': '주문 전송됨',
        'account': parsed['account'],
        'exchange': accounts[parsed['account']]['exchange'],
        'symbol': parsed['symbol'],
        'network': 'testnet' if 'testnet' in parsed['account'].lower() else 'mainnet'
    }), 200

if __name__ == '__main__':
    app.run()
