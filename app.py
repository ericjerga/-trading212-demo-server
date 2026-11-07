"""Synthetic-data demo trading simulator with a password-protected mobile dashboard.
No broker orders. All simulator state is in memory and resets on service restart.
"""
import os
import math
import random
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import secrets
import threading
from functools import wraps
from flask import Flask, jsonify, request, Response, redirect, url_for, session, render_template_string

app = Flask(__name__)
# A stable session signing key is derived from the existing dashboard token.
# Change DASHBOARD_TOKEN in Render and GitHub Actions if it was exposed.
import hashlib
app.secret_key = hashlib.sha256(('dashboard-session-v1:' + os.environ.get('DASHBOARD_TOKEN', '')).encode()).digest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=True)
LOCK = threading.Lock()
START = 1000.0
state = {'cash': START, 'shares': 0.0, 'price': 100.0, 'tick': 0, 'trades': [], 'halted': False, 'peak': START, 'news': [], 'news_checked': None, 'news_error': None, 'last_decision': 'Noch keine Prüfung', 'news_score': None}

def valid_token(value):
    token = os.environ.get('DASHBOARD_TOKEN', '')
    return bool(token) and secrets.compare_digest(value, token)

def authorized():
    return valid_token(request.headers.get('X-Dashboard-Token', ''))

def snapshot():
    equity = state['cash'] + state['shares'] * state['price']
    return {**state, 'equity': round(equity, 2), 'cash': round(state['cash'], 2), 'price': round(state['price'], 4), 'shares': round(state['shares'], 5), 'environment': 'synthetic_simulation', 'real_orders_enabled': False, 'trading212_connected': False}

@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response

@app.get('/')
def home():
    return redirect(url_for('dashboard'))

@app.get('/health')
def health():
    return jsonify(status='ok', real_orders_enabled=False, environment='synthetic_simulation')

@app.get('/status')
def status():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    with LOCK: return jsonify(snapshot())

# Öffentliche RSS-Schlagzeilen, ohne API-Schlüssel. Nur eine grobe Heuristik,
# keine KI-Prognose. Bei fehlenden/veralteten Nachrichten keine neuen Käufe.
RSS_URL = 'https://news.google.com/rss/search?q=Aktienmarkt+Wirtschaft+when%3A1d&hl=de&gl=DE&ceid=DE:de'
POSITIVE = ('steigt', 'steigen', 'wachstum', 'rekord', 'gewinne', 'erholung', 'aufschwung', 'rally', 'growth', 'beats')
NEGATIVE = ('crash', 'einbruch', 'krise', 'verlust', 'rezession', 'warnung', 'inflation', 'entlassung', 'senkt', 'fällt', 'fall', 'plunge', 'misses')

def fetch_news():
    req = urllib.request.Request(RSS_URL, headers={'User-Agent':'Mozilla/5.0 DemoResearchBot/1.0', 'Accept':'application/rss+xml, application/xml'})
    with urllib.request.urlopen(req, timeout=9) as resp:
        raw = resp.read(250000)
    root = ET.fromstring(raw)
    now = datetime.now(timezone.utc)
    items = []
    for item in root.findall('./channel/item')[:25]:
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        pub = item.findtext('pubDate') or ''
        try:
            date = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        if not title or date > now + timedelta(minutes=5) or now-date > timedelta(hours=24):
            continue
        lower = title.casefold()
        score = sum(x in lower for x in POSITIVE) - sum(x in lower for x in NEGATIVE)
        items.append({'title':title[:180], 'url':link if link.startswith('https://') else '', 'published':date.strftime('%d.%m. %H:%M UTC'), 'score':score})
        if len(items) >= 10: break
    if len(items) < 3:
        raise ValueError('Zu wenige aktuelle Nachrichten verfügbar')
    return items, sum(i['score'] for i in items)

@app.post('/step')
def step():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    # Netzwerkzugriff außerhalb des Locks: die Seite bleibt währenddessen erreichbar.
    try:
        news, score = fetch_news()
        error = None
    except Exception:
        news, score, error = [], None, 'Nachrichten momentan nicht abrufbar – keine neuen Käufe'
    with LOCK:
        state['news_checked'] = datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
        state['news'] = news
        state['news_score'] = score
        state['news_error'] = error
        if state['halted']:
            state['last_decision'] = 'Simulation durch Risikogrenze gestoppt'
            return jsonify(snapshot())
        state['tick'] += 1
        rng = random.Random(20261107 + state['tick'])
        drift = 0.0001 + 0.007 * math.sin(state['tick']/17)
        state['price'] = max(0.01, state['price'] * (1 + drift + rng.uniform(-0.018, 0.018)))
        equity = state['cash'] + state['shares'] * state['price']
        state['peak'] = max(state['peak'], equity)
        if equity < state['peak'] * 0.95:
            state['halted'] = True
            state['last_decision'] = '5 % Rückgang vom Höchststand: Simulation angehalten'
        elif state['shares'] > 0 and (score is not None and score <= -2 or state['tick'] % 8 == 4):
            reason = 'negative Nachrichtenlage' if score is not None and score <= -2 else 'planmäßiger Demo-Ausstieg'
            amount = state['shares'] * state['price']
            state['cash'] += amount
            state['shares'] = 0.0
            state['trades'].append({'step':state['tick'],'side':'SIM_SELL','price':round(state['price'],4),'amount':round(amount,2),'reason':reason})
            state['last_decision'] = 'Virtueller Verkauf: ' + reason
        elif state['shares'] == 0 and state['tick'] % 8 == 0 and score is not None and score >= 2:
            spend = min(state['cash'], equity * 0.10)
            if spend > 0:
                state['shares'] = spend / state['price']
                state['cash'] -= spend
                state['trades'].append({'step':state['tick'],'side':'SIM_BUY','price':round(state['price'],4),'amount':round(spend,2),'reason':'positive Nachrichten-Schlagwörter (Demo-Heuristik)'})
                state['last_decision'] = 'Virtueller Kauf: positive Schlagwörter; max. 10 %'
        else:
            state['last_decision'] = error or ('Abwarten: Nachrichten-Score %s; kein ausreichendes Kaufsignal' % score)
        state['trades'] = state['trades'][-50:]
        return jsonify(snapshot())

@app.post('/stop')
def stop():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    with LOCK:
        state['halted'] = True
        return jsonify(snapshot())

PAGE = '''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#101827"><title>Mein Demo-Trading-Bot</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#f3f6fc;font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:600px;margin:auto;padding:28px 18px 70px}h1{font-size:27px;margin:6px 0 6px}.sub{color:#a7b4c9;font-size:14px;margin:0 0 26px}.card{background:#172238;border:1px solid #293650;border-radius:18px;padding:19px;margin-bottom:13px}.label{font-size:13px;color:#acbad0}.big{font-size:34px;font-weight:750;margin:8px 0 2px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.grid .card{min-width:0}.grid .big{font-size:23px}.positive{color:#7ee0aa}.negative{color:#ff9b9b}.pill{display:inline-block;border-radius:99px;background:#26354d;padding:7px 11px;font-size:13px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:12px 5px;text-align:left;border-bottom:1px solid #304059}th{color:#acbad0;font-weight:500}td:last-child,th:last-child{text-align:right}button{background:#3c83f6;color:white;border:0;border-radius:12px;padding:14px 17px;font-weight:650;font-size:16px;width:100%;cursor:pointer}input{background:#0e182b;border:1px solid #40506b;color:white;padding:14px;border-radius:12px;width:100%;font-size:16px;margin:12px 0}a{color:#a9c8ff}.foot{font-size:12px;color:#91a0b7;line-height:1.6;margin-top:24px}.news-item{padding:11px 0;border-bottom:1px solid #304059;font-size:14px;line-height:1.4}.news-item:last-child{border:0}.row{display:flex;align-items:center;justify-content:space-between;gap:12px}.logout{font-size:13px}.empty{color:#acbad0;font-size:14px}details.trade-list{margin-top:10px}summary.trade-toggle{list-style:none;background:#263c60;color:#f3f6fc;border-radius:12px;padding:15px;font-weight:700;cursor:pointer;text-align:center}summary.trade-toggle::-webkit-details-marker{display:none}summary.trade-toggle:after{content:"  ▾"}details[open] summary.trade-toggle:after{content:"  ▴"}.trade-content{margin-top:14px;overflow-x:auto}.trade-item{border-bottom:1px solid #304059;padding:13px 0}.trade-item:last-child{border:0}.trade-head{display:flex;justify-content:space-between;gap:8px;font-weight:650;margin-bottom:7px}.trade-meta{font-size:13px;color:#acbad0;line-height:1.65}.buy{color:#7ee0aa}.sell{color:#ffca86}
</style></head><body><main>{% if login %}<h1>🤖 Mein Demo-Trading-Bot</h1><p class="sub">Bitte mit deinem Dashboard-Passwort anmelden.</p><div class="card"><form method="post" action="{{ url_for('dashboard_login') }}"><label for="token">Dashboard-Passwort</label><input id="token" name="token" type="password" autocomplete="current-password" required><button type="submit">Anmelden</button></form>{% if error %}<p class="negative">Passwort falsch. Bitte erneut versuchen.</p>{% endif %}</div>{% else %}<div class="row"><h1>🤖 Mein Trading-Bot</h1><a class="logout" href="{{ url_for('dashboard_logout') }}">Abmelden</a></div><p class="sub">Virtuelle Simulation · Keine echten Aktienkäufe</p><div class="card"><div class="label">Virtueller Kontostand</div><div class="big">{{ '%.2f'|format(data.equity) }} €</div><span class="pill">{{ '⛔ Gestoppt' if data.halted else '🟢 Simulation bereit' }}</span></div><div class="grid"><div class="card"><div class="label">Gewinn / Verlust</div><div class="big {{ 'positive' if gain >= 0 else 'negative' }}">{{ '%+.2f'|format(gain) }} €</div></div><div class="card"><div class="label">Simulationsschritte</div><div class="big">{{ data.tick }}</div></div><div class="card"><div class="label">Verfügbares Geld</div><div class="big">{{ '%.2f'|format(data.cash) }} €</div></div><div class="card"><div class="label">Virtuelle Position</div><div class="big">{{ '%.2f'|format(data.shares * data.price) }} €</div></div></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">Käufe und Verkäufe</h2><p class="empty">{{ data.trades|length }} simulierte Trades seit dem letzten Serverstart</p><details class="trade-list"><summary class="trade-toggle">📋 Käufe &amp; Verkäufe anzeigen</summary><div class="trade-content">{% if data.trades %}{% for trade in data.trades|reverse %}<div class="trade-item"><div class="trade-head"><span class="{{ 'buy' if trade.side == 'SIM_BUY' else 'sell' }}">{{ '🟢 Kauf' if trade.side == 'SIM_BUY' else '🟠 Verkauf' }}</span><span>{{ '%.2f'|format(trade.amount) }} €</span></div><div class="trade-meta">Grund: {{ trade.reason|default('Frühere Demo-Regel') }}<br>Simulationsschritt: {{ trade.step }}<br>Preis pro Anteil: {{ '%.4f'|format(trade.price) }} €<br>Anteile: {{ '%.5f'|format(trade.amount / trade.price) if trade.price else '—' }}</div></div>{% endfor %}{% else %}<p class="empty">Noch keine simulierten Käufe oder Verkäufe. Der erste Kauf ist frühestens bei Simulationsschritt 8 vorgesehen.</p>{% endif %}</div></details></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">📰 Nachrichten &amp; Entscheidungen</h2><p class="empty">Letzte Prüfung: {{ data.news_checked or 'Noch nicht geprüft' }}</p><p><strong>Letzte Entscheidung:</strong> {{ data.last_decision }}</p>{% if data.news_error %}<p class="negative">{{ data.news_error }}</p>{% endif %}<p class="empty">Nachrichten-Score: {{ data.news_score if data.news_score is not none else 'Nicht verfügbar' }} · Schlagwort-Heuristik, keine verlässliche Marktprognose</p><details class="trade-list"><summary class="trade-toggle">📰 Nachrichten anzeigen</summary><div class="trade-content">{% if data.news %}{% for item in data.news %}<div class="news-item">{% if item.url %}<a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}<br><span class="empty">{{ item.published }}</span></div>{% endfor %}{% else %}<p class="empty">Noch keine aktuellen Nachrichten gespeichert.</p>{% endif %}</div></details></div><button type="button" onclick="location.reload()">🔄 Werte aktualisieren</button><p class="foot">Startkapital: 1.000 € · Künstliche Kurse, keine Trading-212-Verbindung. Nachrichten sind nur ein Filter für Demo-Entscheidungen, keine realen Aktienempfehlungen. Die Anzeige beweist nicht, dass GitHub regelmäßig startet. Auf dem kostenlosen Render-Dienst kann der Simulationsstand bei Neustart oder Neuveröffentlichung zurückgesetzt werden.</p>{% endif %}</main></body></html>'''

@app.get('/dashboard')
def dashboard():
    if not session.get('dashboard_logged_in'):
        return render_template_string(PAGE, login=True, error=False)
    with LOCK:
        data = snapshot()
    return render_template_string(PAGE, login=False, data=data, gain=data['equity']-START)

@app.post('/dashboard/login')
def dashboard_login():
    if valid_token(request.form.get('token', '')):
        session.clear()
        session['dashboard_logged_in'] = True
        return redirect(url_for('dashboard'))
    return render_template_string(PAGE, login=True, error=True), 401

@app.get('/dashboard/logout')
def dashboard_logout():
    session.clear()
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
