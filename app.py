# app.py – V37 완전 호환 (Bitget + Bybit 한 줄 입력)
from flask import Flask, request, jsonify
import requests, json, threading, os, hashlib, hmac
from datetime import datetime

app = Flask(__name__)

# ----------------------------------------------------------------------
# 1. 한 줄로 계정 로드 (Bitget: 5개, Bybit: 4개)
# ----------------------------------------------------------------------
accounts = {}
raw = os.getenv('EXCHANGE_ACCOUNTS', '')
for line in raw.strip().split('\n'):
    p = [x.strip() for x in line.split(',')]
    if len(p) < 4: continue
    uid, exch, key, secret = p[0], p[1].lower(), p[2], p[3]
    passphrase = p[4] if len(p) > 4 else ''
    accounts[uid] = {
        'exchange': exch,          # bitget / bybit
        'key': key,
        'secret': secret,
        'passphrase': passphrase   # Bybit 은 빈 문자열
    }

# ----------------------------------------------------------------------
# 2. V37 메시지 파싱
# ----------------------------------------------------------------------
def parse_v37(msg: str):
    if not (msg.startswith('TVM:') and msg.endswith(':MVT')):
        return None
    try:
        payload = json.loads(msg[4:-4])

        exchange = payload.get('exchange', '').lower()
        account  = payload.get('account', '')
        symbol   = payload.get('symbol', '').replace('/', 'USDT')
        side_raw = payload.get('side', '').lower()          # buy / sell (소문자)
        bal_pct  = float(payload.get('bal_pct', 0))
        leverage = int(payload.get('leverage', 1))
        margin   = payload.get('margin_type', 'cross')
        token    = payload.get('token', '')
        trail    = float(payload.get('trailing_stop', 0))
        ts_price = float(payload.get('ts_ac_price', 0))

        # direction (Bitget 내부용)
        if 'buy' in side_raw:
            direction = 'open_long' if 'close' not in side_raw else 'close_short'
        elif 'sell' in side_raw:
            direction = 'open_short' if 'close' not in side_raw else 'close_long'
        else:
            return None

        return {
            'exchange'   : exchange,
            'account'    : account,
            'symbol'     : symbol,
            'direction'  : direction,      # Bitget 전용
            'side'       : side_raw,       # Bybit 전용 (buy/sell)
            'bal_pct'    : bal_pct,
            'leverage'   : leverage,
            'margin_type': margin,
            'token'      : token,
            'trailing'   : trail,
            'ts_price'   : ts_price
        }
    except Exception as e:
        print('Parse error →', e)
        return None

# ----------------------------------------------------------------------
# 3. Bitget 선물 주문
# ----------------------------------------------------------------------
def bitget_order(data):
    acc = accounts[data['account']]
    if acc['exchange'] != 'bitget': return {'error':'Not Bitget'}

    def sign(m, u, b, t):
        p = f"{t}{m.upper()}{u}{json.dumps(b) if b else ''}"
        return hmac.new(acc['secret'].encode(), p.encode(), hashlib.sha256).hexdigest()

    ts = str(int(datetime.now().timestamp()*1000))

    # ---- 레버리지 ----
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

    # ---- 마진 타입 (isolated) ----
    if data['margin_type']=='isolated':
        m_url = '/api/mix/v1/account/setMarginMode'
        m_body = {'symbol':data['symbol'],'marginMode':'isolated'}
        t2 = str(int(datetime.now().timestamp()*1000))
        hdr2 = {**hdr, 'ACCESS-SIGN':sign('POST',m_url,m_body,t2), 'ACCESS-TIMESTAMP':t2}
        requests.post('https://api.bitget.com'+m_url, headers=hdr2, json=m_body)

    # ---- 잔고 ----
    bal_res = requests.get(
        'https://api.bitget.com/api/mix/v1/account/accounts',
        headers={**hdr, 'ACCESS-SIGN':sign('GET','/api/mix/v1/account/accounts','',ts)}
    ).json()
    usdt = next((x for x in bal_res.get('data',[]) if x['marginCoin']=='USDT'),{}).get('available','0')

    # ---- 현재가 ----
    price = float(requests.get(
        f'https://api.bitget.com/api/mix/v1/market/ticker?symbol={data["symbol"]}'
    ).json()['data']['lastPrice'])

    qty = float(usdt) * data['bal_pct'] / 100 / price

    # ---- 주문 ----
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
# 4. Bybit 선물 주문 (테스트넷·메인넷 자동 구분)
# ----------------------------------------------------------------------
def bybit_order(data):
    acc = accounts[data['account']]
    if acc['exchange'] != 'bybit': return {'error':'Not Bybit'}

    base = 'https://api-testnet.bybit.com' if 'test' in acc['key'].lower() else 'https://api.bybit.com'
    api_key, secret = acc['key'], acc['secret']

    def sign(params, ts):
        p = f"{api_key}{ts}5000{json.dumps(params) if isinstance(params,dict) else params}"
        return hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()

    ts = str(int(datetime.now().timestamp()*1000))

    # ---- 레버리지 ----
    lev = {'category':'linear','symbol':data['symbol'],
           'buyLeverage':str(data['leverage']),'sellLeverage':str(data['leverage'])}
    hdr = {
        'X-BAPI-API-KEY'    : api_key,
        'X-BAPI-SIGN'       : sign(lev,ts),
        'X-BAPI-TIMESTAMP'  : ts,
        'X-BAPI-RECV-WINDOW': '5000',
        'Content-Type'      : 'application/json'
    }
    requests.post(f'{base}/v5/position/set-leverage', headers=hdr, json=lev)

    # ---- 마진 타입 ----
    mm = {'category':'linear','symbol':data['symbol'],'marginMode':data['margin_type']}
    hdr_m = {**hdr, 'X-BAPI-SIGN':sign(mm,ts)}
    requests.post(f'{base}/v5/account/set-margin-mode', headers=hdr_m, json=mm)

    # ---- 잔고 ----
    bal_res = requests.get(
        f'{base}/v5/account/wallet-balance',
        headers={**hdr, 'X-BAPI-SIGN':sign({'category':'linear'},ts)},
        params={'category':'linear'}
    ).json()
    usdt = next((x for x in bal_res.get('result',{}).get('list',[]) if x['coin']=='USDT'),{}).get('walletBalance','0')

    # ---- 현재가 ----
    price_res = requests.get(f'{base}/v5/market/tickers', params={'category':'linear','symbol':data['symbol']}).json()
    price = float(price_res['result']['list'][0]['lastPrice'])

    qty = float(usdt) * data['bal_pct'] / 100 / price

    # ---- 주문 ----
    order = {
        'category' : 'linear',
        'symbol'   : data['symbol'],
        'side'     : 'Buy' if 'buy' in data['side'] else 'Sell',   # 대문자
        'orderType': 'Market',
        'qty'      : str(round(qty,6))
    }
    if data['trailing']>0:
        order['tpslMode'] = 'FullMode'
        order['tpTriggerBy'] = 'LastPrice'
        order['slTriggerBy'] = 'LastPrice'
        order['slRate'] = str(data['trailing'])

    hdr_o = {**hdr, 'X-BAPI-SIGN':sign(order,ts)}
    return requests.post(f'{base}/v5/order/create', headers=hdr_o, json=order).json()

# ----------------------------------------------------------------------
# 5. 웹훅 엔드포인트
# ----------------------------------------------------------------------
@app.route('/order', methods=['POST'])
def webhook():
    payload = request.get_json(silent=True) or {}
    msg = payload.get('message','')
    parsed = parse_v37(msg)

    if not parsed or parsed['account'] not in accounts:
        return jsonify({'error':'Invalid V37 message'}), 400

    def run():
        exch = accounts[parsed['account']]['exchange']
        result = bybit_order(parsed) if exch=='bybit' else bitget_order(parsed)
        print(f'V37 [{parsed["account"]} {exch}]:', result)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({
        'status'   : '주문 전송됨',
        'account'  : parsed['account'],
        'exchange' : accounts[parsed['account']]['exchange'],
        'symbol'   : parsed['symbol']
    }), 200

if __name__ == '__main__':
    app.run()
