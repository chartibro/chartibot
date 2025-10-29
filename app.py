# app.py – V37 완전 호환 + Bybit USDT Perpetual 완벽 주문 + 디버그 로그
from flask import Flask, request, jsonify
import requests, json, threading, os, hashlib, hmac
from datetime import datetime

app = Flask(__name__)

# ----------------------------------------------------------------------
# 1. 한 줄 계정 로드 (Bitget 5개 / Bybit 4개)
# ----------------------------------------------------------------------
accounts = {}
raw = os.getenv('EXCHANGE_ACCOUNTS', '')
print(f"[DEBUG] EXCHANGE_ACCOUNTS 원본: {repr(raw)}")
for line in raw.strip().split('\n'):
    p = [x.strip() for x in line.split(',')]
    if len(p) < 4: 
        print(f"[DEBUG] 무시된 줄 (형식 오류): {line}")
        continue
    uid, exch, key, secret = p[0], p[1].lower(), p[2], p[3]
    passphrase = p[4] if len(p) > 4 else ''
    accounts[uid] = {
        'exchange': exch,
        'key': key,
        'secret': secret,
        'passphrase': passphrase
    }
print(f"[DEBUG] 등록된 계정: {list(accounts.keys())}")

# ----------------------------------------------------------------------
# 2. V37 메시지 파싱
# ----------------------------------------------------------------------
def parse_v37(msg: str):
    print(f"[DEBUG] 수신된 전체 message: {repr(msg)}")
    if not msg.startswith('TVM:'):
        print(f"[DEBUG] TVM: 접두어 없음")
        return None
    if not msg.endswith(':MVT'):
        print(f"[DEBUG] :MVT 접미사 없음")
        return None

    json_part = msg[4:-4]
    print(f"[DEBUG] 추출된 JSON 부분: {repr(json_part)}")

    try:
        payload = json.loads(json_part)
        print(f"[DEBUG] JSON 파싱 성공: {payload}")
    except Exception as e:
        print(f"[DEBUG] JSON 파싱 실패: {e}")
        return None

    exchange = payload.get('exchange', '').lower()
    account  = payload.get('account', '')
    symbol   = payload.get('symbol', '').replace('/', 'USDT')
    side_raw = payload.get('side', '').lower()
    bal_pct  = float(payload.get('bal_pct', 0))
    leverage = int(payload.get('leverage', 1))
    margin   = payload.get('margin_type', 'cross')
    token    = payload.get('token', '')
    trail    = float(payload.get('trailing_stop', 0))
    ts_price = float(payload.get('ts_ac_price', 0))

    if 'buy' in side_raw:
        direction = 'open_long' if 'close' not in side_raw else 'close_short'
    elif 'sell' in side_raw:
        direction = 'open_short' if 'close' not in side_raw else 'close_long'
    else:
        print(f"[DEBUG] side 값 오류: {side_raw}")
        return None

    return {
        'exchange'   : exchange,
        'account'    : account,
        'symbol'     : symbol,
        'direction'  : direction,
        'side_raw'   : side_raw,
        'bal_pct'    : bal_pct,
        'leverage'   : leverage,
        'margin_type': margin,
        'token'      : token,
        'trailing'   : trail,
        'ts_price'   : ts_price
    }

# ----------------------------------------------------------------------
# 3. Bitget 선물 (오타 수정됨)
# ----------------------------------------------------------------------
def bitget_order(data):
    acc = accounts[data['account']]
    if acc['exchange'] != 'bitget': return {'error':'Not Bitget'}

    def sign(m, u, b, t):
        p = f"{t}{m.upper()}{u}{json.dumps(b) if b else ''}"
        return hmac.new(acc['secret'].encode(), p.encode(), hashlib.sha256).hexdigest()

    ts = str(int(datetime.now().timestamp()*1000))

    lev_url = '/api/mix/v1/account/setLeverage'
    lev_body = {'symbol':data['symbol'],'marginCoin':'USDT','leverage':str(data['leverage'])}
    hdr = {
        'ACCESS-KEY'       : acc['key'],
        'ACCESS-SIGN'      : sign('POST',lev_url,lev_body,ts),
        'ACCESS-TIMESTAMP' : ts,
        'ACCESS-PASSPHRASE': acc['passphrase'],
        'Content-Type'     : 'application/json'
    }
    requests.post('https://api.bitget.com'+lev_url, headers=hdr, json=lev_body)

    if data['margin_type']=='isolated':
        m_url = '/api/mix/v1/account/setMarginMode'
        m_body = {'symbol':data['symbol'],'marginMode':'isolated'}
        t2 = str(int(datetime.now().timestamp()*1000))
        hdr2 = {**hdr, 'ACCESS-SIGN':sign('POST',m_url,m_body,t2), 'ACCESS-TIMESTAMP':t2}
        requests.post('https://api.bitget.com'+m_url, headers=hdr2, json=m_body)

    bal_res = requests.get(
        'https://api.bitget.com/api/mix/v1/account/accounts',
        headers={**hdr, 'ACCESS-SIGN':sign('GET','/api/mix/v1/account/accounts','',ts)}
    ).json()
    usdt = next((x for x in bal_res.get('data',[]) if x['marginCoin']=='USDT'), {}).get('available','0')

    price = float(requests.get(
        f'https://api.bitget.com/api/mix/v1/market/ticker?symbol={data["symbol"]}'
    ).json()['data']['lastPrice'])

    qty = float(usdt) * data['bal_pct'] / 100 / price

    order_url = '/api/mix/v1/plan/placeOrder'
    body = {
        'symbol'    : data['symbol'],
        'marginCoin': 'USDT',
        'side'      : data['direction'],
        'orderType' : 'market',
        'size'      : str(round(qty,6)),
        'clientOid' : f'v37_{int(datetime.now().timestamp())}'
    }
    if data['trailing']>0:
        body['triggerPrice'] = str(round(price + data['ts_price'] if 'long' in data['direction'] else price - data['ts_price'],6))
        body['callbackRate'] = str(data['trailing'])

    t3 = str(int(datetime.now().timestamp()*1000))
    hdr3 = {**hdr, 'ACCESS-SIGN':sign('POST',order_url,body,t3), 'ACCESS-TIMESTAMP':t3}
    return requests.post('https://api.bitget.com'+order_url, headers=hdr3, json=body).json()

# ----------------------------------------------------------------------
# 4. Bybit 선물 – USDT Perpetual 완벽 주문 + 디버그
# ----------------------------------------------------------------------
def bybit_order(data):
    acc = accounts[data['account']]
    if acc['exchange'] != 'bybit': 
        print("[ERROR] Not Bybit account")
        return {'error':'Not Bybit'}

    is_testnet = 'testnet' in data['account'].lower() or 'testnet' in acc['key'].lower()
    base = 'https://api-testnet.bybit.com' if is_testnet else 'https://api.bybit.com'
    print(f"[DEBUG] Bybit 네트워크: {'테스트넷' if is_testnet else '실제넷'} ({base})")
    api_key, secret = acc['key'], acc['secret']

    def sign(params, ts):
        p = f"{api_key}{ts}5000{json.dumps(params) if isinstance(params,dict) else params}"
        return hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()

    ts = str(int(datetime.now().timestamp()*1000))

    # === 레버리지 설정 ===
    try:
        lev = {'category':'linear','symbol':data['symbol'],'buyLeverage':str(data['leverage']),'sellLeverage':str(data['leverage'])}
        hdr = {
            'X-BAPI-API-KEY': api_key,
            'X-BAPI-SIGN': sign(lev,ts),
            'X-BAPI-TIMESTAMP': ts,
            'X-BAPI-RECV-WINDOW': '5000',
            'Content-Type': 'application/json'
        }
        lev_res = requests.post(f'{base}/v5/position/set-leverage', headers=hdr, json=lev)
        print(f"[DEBUG] 레버리지 설정 응답: {lev_res.status_code} {lev_res.text}")
    except Exception as e:
        print(f"[ERROR] 레버리지 설정 실패: {e}")
        return {'error': 'leverage failed'}

    # === 마진 모드 ===
    try:
        mm = {'category':'linear','symbol':data['symbol'],'marginMode':data['margin_type']}
        hdr_m = {**hdr, 'X-BAPI-SIGN':sign(mm,ts)}
        mm_res = requests.post(f'{base}/v5/account/set-margin-mode', headers=hdr_m, json=mm)
        print(f"[DEBUG] 마진 모드 응답: {mm_res.status_code} {mm_res.text}")
    except Exception as e:
        print(f"[ERROR] 마진 모드 실패: {e}")

    # === 잔고 조회 ===
    try:
        bal_res = requests.get(
            f'{base}/v5/account/wallet-balance',
            headers={**hdr, 'X-BAPI-SIGN':sign({'category':'linear'},ts)},
            params={'category':'linear'}
        )
        bal_json = bal_res.json()
        print(f"[DEBUG] 잔고 응답: {bal_json}")
        usdt = next((x for x in bal_json.get('result',{}).get('list',[]) if x['coin']=='USDT'),{}).get('walletBalance','0')
        print(f"[DEBUG] USDT 잔고: {usdt}")
        if float(usdt) <= 0:
            print(f"[ERROR] 잔고 0")
            return {'error': 'zero balance'}
    except Exception as e:
        print(f"[ERROR] 잔고 조회 실패: {e}")
        return {'error': 'balance failed'}

    # === 현재가 조회 ===
    try:
        price_res = requests.get(f'{base}/v5/market/tickers', params={'category':'linear','symbol':data['symbol']})
        price_json = price_res.json()
        print(f"[DEBUG] 티커 응답: {price_json}")
        price = float(price_json['result']['list'][0]['lastPrice'])
        print(f"[DEBUG] 현재가: {price}")
    except Exception as e:
        print(f"[ERROR] 티커 조회 실패: {e}")
        return {'error': 'ticker failed'}

    # === 수량 계산 ===
    try:
        qty = float(usdt) * data['bal_pct'] / 100 / price
        qty = round(qty, 6)
        if qty <= 0:
            print(f"[ERROR] 계산된 수량 0: {qty}")
            return {'error': 'qty zero'}
        print(f"[DEBUG] 주문 수량: {qty}")
    except Exception as e:
        print(f"[ERROR] 수량 계산 실패: {e}")
        return {'error': 'qty calc failed'}

    # === 주문 전송 ===
    try:
        order = {
            'category': 'linear',
            'symbol': data['symbol'],
            'side': 'Buy' if 'buy' in data['side_raw'] else 'Sell',
            'orderType': 'Market',
            'qty': str(qty)
        }
        hdr_o = {**hdr, 'X-BAPI-SIGN':sign(order,ts)}
        order_res = requests.post(f'{base}/v5/order/create', headers=hdr_o, json=order)
        result = order_res.json()
        print(f"[BYBIT ORDER RESULT] {result}")
        return result
    except Exception as e:
        print(f"[ERROR] 주문 전송 실패: {e}")
        return {'error': 'order failed'}

# ----------------------------------------------------------------------
# 5. 웹훅
# ----------------------------------------------------------------------
@app.route('/order', methods=['POST'])
def webhook():
    payload = request.get_json(silent=True) or {}
    msg = payload.get('message', '')
    
    print(f"[WEBHOOK] 수신된 전체 payload: {payload}")
    print(f"[WEBHOOK] message 값: {repr(msg)}")

    parsed = parse_v37(msg)

    if not parsed:
        print(f"[ERROR] V37 파싱 실패")
        return jsonify({'error':'Invalid V37 message'}), 400

    if parsed['account'] not in accounts:
        print(f"[ERROR] 계정 없음: {parsed['account']}, 등록 계정: {list(accounts.keys())}")
        return jsonify({'error':'Account not found'}), 400

    print(f"[SUCCESS] 파싱 성공: {parsed}")

    def run():
        exch = accounts[parsed['account']]['exchange']
        result = bybit_order(parsed) if exch=='bybit' else bitget_order(parsed)
        print(f"V37 주문 결과 [{parsed['account']} {exch}]: {result}")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({
        'status'   : '주문 전송됨',
        'account'  : parsed['account'],
        'exchange' : accounts[parsed['account']]['exchange'],
        'symbol'   : parsed['symbol'],
        'network'  : 'testnet' if 'testnet' in parsed['account'].lower() else 'mainnet'
    }), 200

if __name__ == '__main__':
    app.run()
