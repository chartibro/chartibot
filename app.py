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
    accounts[uid] = {
        'exchange': exch.lower(),
        'key': key,
        'secret': secret,
        'passphrase': passphrase
    }

# === V38 메시지 파싱 ===
def parse_v38(msg: str):
    if not msg.startswith('TVM:') or not msg.endswith(':MVT'):
        return None
    try:
        payload = json.loads(msg[4:-4])
    except:
        return None

    # 필수 필드
    exchange = payload.get('exchange', '').lower()
    account = payload.get('account', '')
    symbol = payload.get('symbol', '').replace('/', 'USDT')  # BTC/USDT → BTCUSDT
    side = payload.get('side', '').lower()
    bal_pct = float(payload.get('bal_pct', 0))
    leverage = int(payload.get('leverage', 1))
    margin_type = payload.get('margin_type', 'cross').lower()
    token = payload.get('token', '')

    # side 변환
    if 'buy' in side and 'close' in side:
        direction = 'close_short'
    elif 'sell' in side and 'close' in side:
        direction = 'close_long'
    elif 'buy' in side:
        direction = 'open_long'
    elif 'sell' in side:
        direction = 'open_short'
    else:
        return None

    return {
        'exchange': exchange,
        'account': account,
        'symbol': symbol,
        'direction': direction,
        'bal_pct': bal_pct,
        'leverage': leverage,
        'margin_type': margin_type,
        'token': token,
        'same_order': payload.get('same_order', ''),
        'trailing_stop': payload.get('trailing_stop'),
        'ts_ac_price': payload.get('ts_ac_price'),
        'position_close': payload.get('position_close', False)
    }

# === Bitget 서명 (공식 방식) ===
def bitget_sign(ts, method, url, body, secret):
    body_str = json.dumps(body) if body else ''
    pre_hash = f"{ts}{method.upper()}{url}{body_str}"
    logger.info(f"[SIGN] {pre_hash}")
    return hmac.new(secret.encode('utf-8'), pre_hash.encode('utf-8'), hashlib.sha256).hexdigest()

# === 현재가 조회 (bal_pct → size 계산용) ===
def get_price(symbol):
    try:
        url = f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={symbol}&productType=UMCBL_USDT"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get('code') == '00000':
            return float(data['data'][0]['lastPr'])
    except Exception as e:
        logger.error(f"[PRICE ERROR] {e}")
    return None

# === 계좌 잔액 조회 (bal_pct 계산용) ===
def get_balance(acc):
    try:
        ts = str(int(datetime.now().timestamp() * 1000))
        url = '/api/v2/mix/account/accounts'
        body = {"productType": "UMCBL_USDT"}
        sign = bitget_sign(ts, 'POST', url, body, acc['secret'])
        headers = {
            'ACCESS-KEY': acc['key'],
            'ACCESS-TIMESTAMP': ts,
            'ACCESS-PASSPHRASE': acc['passphrase'],
            'ACCESS-SIGN': sign,
            'Content-Type': 'application/json',
            'locale': 'en-US'
        }
        r = requests.post('https://api.bitget.com' + url, headers=headers, json=body, timeout=15)
        data = r.json()
        if data.get('code') == '00000':
            for item in data['data']:
                if item['marginCoin'] == 'USDT':
                    return float(item['available'])
    except Exception as e:
        logger.error(f"[BALANCE ERROR] {e}")
    return None

# === Bitget 주문 ===
def bitget_order(data):
    try:
        acc = accounts.get(data['account'])
        if not acc or acc['exchange'] != 'bitget':
            return {'error': 'Invalid account'}

        symbol = data['symbol']
        leverage = data['leverage']
        direction = data['direction']
        bal_pct = data['bal_pct']

        # 1. 레버리지 설정
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

        # 2. 잔액 조회 → size 계산
        balance = get_balance(acc)
        if not balance or balance <= 0:
            return {'error': 'Failed to get balance'}

        price = get_price(symbol)
        if not price:
            return {'error': 'Failed to get price'}

        # USDT 기준 주문 금액
        order_usdt = balance * (bal_pct / 100.0) * leverage

        # 계약 크기 (Bitget 선물)
        contract_size = 1.0  # 기본값
        base = symbol.replace('USDT', '')
        if base == 'BTC': contract_size = 0.001
        elif base == 'ETH': contract_size = 0.01
        # DOGE, SOL 등은 1.0

        size = round(order_usdt / price / contract_size, 6)
        if size < 0.001: size = 0.001

        # 3. 주문 실행
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
        logger.info(f"[PARSE FAIL] {msg}")
        return jsonify({'error': 'Invalid TVM format'}), 400

    if parsed['account'] not in accounts:
        return jsonify({'error': 'Unknown account'}), 400

    result = bitget_order(parsed)
    return jsonify({'status': 'ok', 'result': result}), 200

if __name__ == '__main__':
    app.run()
