"""Safe, synthetic-data demo trading simulator. No broker orders, no secrets required."""
import os, time, math, random, secrets, threading
from flask import Flask, jsonify, request, Response

app = Flask(__name__)
LOCK = threading.Lock()
START = 1000.0
state = {'cash': START, 'shares': 0.0, 'price': 100.0, 'tick': 0, 'trades': [], 'halted': False, 'peak': START}

def authorized():
    token = os.environ.get('DASHBOARD_TOKEN', '')
    return bool(token) and secrets.compare_digest(request.headers.get('X-Dashboard-Token', ''), token)

def snapshot():
    equity = state['cash'] + state['shares'] * state['price']
    return {**state, 'equity': round(equity, 2), 'cash': round(state['cash'], 2), 'price': round(state['price'], 4), 'shares': round(state['shares'], 5), 'environment': 'synthetic_simulation', 'real_orders_enabled': False, 'trading212_connected': False}

@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/')
def home():
    return Response('Demo simulator is running. /health is public; /status and /step require X-Dashboard-Token. No Trading 212 orders.', mimetype='text/plain')

@app.get('/health')
def health():
    return jsonify(status='ok', real_orders_enabled=False, environment='synthetic_simulation')

@app.get('/status')
def status():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    with LOCK: return jsonify(snapshot())

@app.post('/step')
def step():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    with LOCK:
        if state['halted']: return jsonify(snapshot())
        state['tick'] += 1
        rng = random.Random(20261107 + state['tick'])
        drift = 0.0001 + 0.007 * math.sin(state['tick']/17)
        state['price'] = max(0.01, state['price'] * (1 + drift + rng.uniform(-0.018, 0.018)))
        equity = state['cash'] + state['shares'] * state['price']
        state['peak'] = max(state['peak'], equity)
        if equity < state['peak'] * 0.95:
            state['halted'] = True
        elif state['tick'] % 8 == 0 and state['shares'] == 0:
            spend = min(state['cash'], equity * 0.10)
            state['shares'] = spend / state['price']
            state['cash'] -= spend
            state['trades'].append({'step':state['tick'],'side':'SIM_BUY','price':round(state['price'],4),'amount':round(spend,2)})
        elif state['tick'] % 8 == 4 and state['shares'] > 0:
            amount = state['shares'] * state['price']
            state['cash'] += amount
            state['shares'] = 0.0
            state['trades'].append({'step':state['tick'],'side':'SIM_SELL','price':round(state['price'],4),'amount':round(amount,2)})
        state['trades'] = state['trades'][-50:]
        return jsonify(snapshot())

@app.post('/stop')
def stop():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    with LOCK:
        state['halted'] = True
        return jsonify(snapshot())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
