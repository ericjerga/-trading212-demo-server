import os
import secrets
from flask import Flask, jsonify, request
import requests

app = Flask(__name__)
BASE = 'https://demo.trading212.com/api/v0'

@app.after_request
def headers(resp):
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

@app.get('/')
def index():
    return jsonify(service='Trading 212 Demo Read-Only', status='running', trading_enabled=False)

@app.get('/health')
def health():
    return jsonify(status='ok', trading_enabled=False)

@app.get('/account')
def account():
    token = os.getenv('DASHBOARD_TOKEN', '')
    provided = request.headers.get('X-Dashboard-Token', '')
    if not token or not secrets.compare_digest(provided, token):
        return jsonify(error='Unauthorized'), 401
    key = os.getenv('T212_API_KEY', '')
    secret = os.getenv('T212_API_SECRET', '')
    if not key or not secret:
        return jsonify(error='Demo API credentials not configured'), 503
    try:
        response = requests.get(BASE + '/equity/account/cash', auth=(key, secret), timeout=12)
        response.raise_for_status()
        return jsonify(account=response.json(), environment='demo', trading_enabled=False)
    except requests.RequestException:
        return jsonify(error='Trading 212 request failed. Check demo credentials and permissions.'), 502

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
