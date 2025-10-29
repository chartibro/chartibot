# app.py – bitget 계정 정상 주문 + 로그 완벽
import logging
from flask import Flask, request, jsonify
import requests, json, threading, os, hashlib, hmac
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 1. 계정 로드
accounts = {}
raw = os.getenv('EXCHANGE_ACCOUNTS', '')
logger.info(f"[INIT] EXCHANGE_ACCOUNTS: {raw}")
for line in raw.strip().split('\n'):
    p = [x.strip() for x in line.split(',')]
    if len(p) < 4: continue
    uid, exch, key, secret = p[0], p[1].lower(), p[2], p[3]
    passphrase = p[4] if len(p) > 4 else ''
    accounts[uid] = {'exchange': exch, 'key': key, 'secret': secret, 'passphrase': passphrase}
logger.info(f"[INIT] 등록된 계정: {list(accounts.keys())}")

# 2. V37 파싱
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
    else: return None

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

# 3. Bitget 주문
def bitget_order(data):
    try:
        logger.info(f"[BITGET] 주문 시작: {data}")
        acc = accounts[data['account']]
        if acc['exchange'] != 'bitget':
            logger.error("[BITGET] Not Bitget account")
            return {'error': 'Not Bitget'}

        def sign(m, u, b, t):
            p = f"{t}{m.upper()}{u}{json.dumps(b) if b else ''}"
            return hmac.new(acc['secret'].encode(), p.encode(), hashlib.sha256).hexdigest()

        ts = str(int(datetime.now().timestamp()*1000))

        # 레버리지
        lev_url = '/api/mix/v1/account/setLeverage'
        lev_body = {'symbol':data['symbol'],'marginCoin':'USDT','leverage':str(data['leverage'])}
        hdr = {
            'ACCESS-KEY': acc['key'],
            'ACCESS-SIGN': sign('POST',lev_url,lev_body,ts),
            'ACCESS-TIMESTAMP': ts,
            'ACCESS-PASSPHRASE': acc['passphrase'],
            'Content-Type': 'application/json'
        }
        r = requests.post('https://api.bitget.com'+lev_url, headers=hdr, json=lev_body)
        logger.info(f"[BITGET] 레버리지 응답: {r.status_code} {r.text}")

        # 마진 타입
        if data['margin_type']=='isolated':
            m_url = '/api/mix/v1/account/setMarginMode'
            m_body = {'symbol':data['symbol'],'marginMode':'isolated'}
            t2 = str(int(datetime.now().timestamp()*1000))
            hdr2 = {**hdr, 'ACCESS-SIGN':sign('POST',m_url,m_body,t2), 'ACCESS-TIMESTAMP':t2}
            r = requests.post('https://api.bitget.com'+m_url, headers=hdr2, json=m_body)
            logger.info(f"[BITGET] 마진 모드 응답: {r.status_code} {r.text}")

        # 잔고
        r = requests.get('https://api.bitget.com/api/mix/v1/account/accounts', headers={**hdr, 'ACCESS-SIGN':sign('GET','/api/mix/v1/account/accounts','',ts)})
        j = r.json()
        logger.info(f"[BITGET] 잔고 응답: {j}")
        usdt = next((x for x in j.get('data',[]) if x['marginCoin']=='USDT'),{}).get('available','0')
        logger.info(f"[BITGET] USDT 잔고: {usdt}")
        if float(usdt) <= 0:
            logger.error("[BITGET] 잔고 부족")
            return {'error': 'zero balance'}

        # 티커
        r = requests.get(f'https://api.bitget.com/api/mix/v1/market/ticker?symbol={data["symbol"]}')
        j = r.json()
        logger.info(f"[BITGET] 티커 응답: {j}")
        price = float(j['data']['lastPrice'])
        logger.info(f"[BITGET] 현재가: {price}")

        # 수량
        qty = round(float(usdt) * data['bal_pct'] / 100 / price, 6)
        if qty <= 0:
            logger.error(f"[BITGET] 수량 0: {qty}")
            return {'error': 'qty zero'}
        logger.info(f"[BITGET] 주문 수량: {qty}")

        # 주문
        order_url = '/api/mix/v1/plan/placeOrder'
        body = {
            'symbol': data['symbol'],
            'marginCoin': 'USDT',
            'side': data['direction'],
            'orderType': 'market',
            'size': str(qty),
            'clientOid': f'v37_{int(datetime.now().timestamp())}'
        }
        t3 = str(int(datetime.now().timestamp()*1000))
        hdr3 = {**hdr, 'ACCESS-SIGN':sign('POST',order_url,body,t3), 'ACCESS-TIMESTAMP':t3}
        r = requests.post('https://api.bitget.com'+order_url, headers=hdr3, json=body)
        result = r.json()
        logger.info(f"[BITGET ORDER RESULT] {result}")
        return result

    except Exception as e:
        logger.error(f"[BITGET] 치명적 오류: {e}", exc_info=True)
        return {'error': str(e)}

# 4. 웹훅 – bitget_order 정상 호출
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
            exch = accounts[parsed['account']]['exchange']
            logger.info(f"[THREAD] 주문 시작 → {exch} 계정: {parsed['account']}")
            result = bitget_order(parsed) if exch == 'bitget' else bybit_order(parsed)  # 수정!
            logger.info(f"[THREAD] 최종 주문 결과: {result}")
        except Exception as e:
            logger.error(f"[THREAD] 스레드 예외: {e}", exc_info=True)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({
        'status': '주문 전송됨',
        'account': parsed['account'],
        'exchange': accounts[parsed['account']]['exchange'],
        'symbol': parsed['symbol'],
        'network': 'mainnet'
    }), 200

if __name__ == '__main__':
    app.run()
