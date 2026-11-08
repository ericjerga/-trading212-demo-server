"""Synthetic-data demo trading simulator with a password-protected mobile dashboard.
No broker orders. USD-denominated paper ledger, no FX conversion. The paper ledger is restored from the GitHub Actions private state branch on the next scheduled step after restart.
"""
import os
import math
import random
import urllib.request
import json
import csv
import io
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
state = {'cash': START, 'shares': 0.0, 'price': 0.0, 'symbol': None, 'tick': 0, 'trades': [], 'halted': False, 'peak': START, 'news': [], 'news_checked': None, 'news_error': None, 'last_decision': 'Noch keine Prüfung', 'news_score': None, 'market': [], 'market_checked': None, 'market_error': None, 'last_trade_date': None, 'equity_history': [], 'last_step_at': None}

def valid_token(value):
    token = os.environ.get('DASHBOARD_TOKEN', '')
    return bool(token) and secrets.compare_digest(value, token)

def authorized():
    return valid_token(request.headers.get('X-Dashboard-Token', ''))

def snapshot():
    equity = state['cash'] + state['shares'] * state['price']
    return {**state, 'equity': round(equity, 2), 'cash': round(state['cash'], 2), 'price': round(state['price'], 4), 'shares': round(state['shares'], 5), 'environment': 'historical_market_data_paper_trading', 'real_orders_enabled': False, 'trading212_connected': False}

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
    return jsonify(status='ok', real_orders_enabled=False, environment='historical_market_data_paper_trading')

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

# Unentgeltliche historische Tageskurse: Yahoo Chart API, mit Stooq als Fallback.
# Diese Endpunkte sind nicht garantiert verfügbar und können verzögert sein.
# Globale Auswahl: US-notierte Aktien und ADRs, Preise in USD.
WATCHLIST = [
    ('AAPL','Apple (USA)'),('MSFT','Microsoft (USA)'),('NVDA','Nvidia (USA)'),
    ('GOOGL','Alphabet (USA)'),('JPM','JPMorgan (USA)'),
    ('SAP','SAP (Deutschland)'),('ASML','ASML (Niederlande)'),
    ('NVO','Novo Nordisk (Daenemark)'),('SNY','Sanofi (Frankreich)'),
    ('UL','Unilever (Europa)'),('AZN','AstraZeneca (UK)'),
    ('TM','Toyota (Japan)'),('SONY','Sony (Japan)'),
    ('TSM','TSMC (Taiwan)'),('BABA','Alibaba (China)'),
    ('INFY','Infosys (Indien)'),('VALE','Vale (Brasilien)'),
    ('BHP','BHP (Australien)')
]

def fetch_closes(symbol):
    yahoo_url = 'https://query1.finance.yahoo.com/v8/finance/chart/%s?range=6mo&interval=1d' % symbol
    try:
        req = urllib.request.Request(yahoo_url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=7) as resp:
            data = json.load(resp)
        result = data['chart']['result'][0]
        times = result['timestamp']
        closes = result['indicators']['quote'][0]['close']
        rows = [(datetime.fromtimestamp(t, timezone.utc).date(), float(c)) for t, c in zip(times, closes) if c is not None and float(c) > 0]
        if len(rows) >= 55:
            return rows, 'Yahoo Finance'
    except Exception:
        pass
    stooq_url = 'https://stooq.com/q/d/l/?s=%s.us&i=d' % symbol.lower()
    req = urllib.request.Request(stooq_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=8) as resp:
        body = resp.read(1000000).decode('utf-8-sig')
    rows = []
    for r in csv.DictReader(io.StringIO(body)):
        try:
            rows.append((datetime.strptime(r['Date'], '%Y-%m-%d').date(), float(r['Close'])))
        except (ValueError, KeyError, TypeError):
            continue
    rows.sort()
    if len(rows) < 55:
        raise ValueError('Nicht genügend historische Kurse')
    return rows, 'Stooq'

def backtest_trend(prices):
    """Illustrative historical test, not a forecast. Previous day's signal."""
    if len(prices) < 55:
        return None
    balance, exposure = 1.0, False
    for i in range(50, len(prices) - 1):
        sma20 = sum(prices[i-19:i+1]) / 20
        sma50 = sum(prices[i-49:i+1]) / 50
        exposure = prices[i] > sma20 > sma50
        if exposure:
            balance *= prices[i+1] / prices[i]
    return {'return_pct': round((balance-1)*100, 2),
            'buy_hold_pct': round((prices[-1]/prices[50]-1)*100, 2)}

def fetch_market():
    today = datetime.now(timezone.utc).date()
    out = []
    for symbol, name in WATCHLIST:
        try:
            rows, source = fetch_closes(symbol)
            latest_date, price = rows[-1]
            if (today - latest_date).days > 5 or latest_date > today:
                raise ValueError('Kursdaten veraltet')
            prices = [x[1] for x in rows]
            sma20 = sum(prices[-20:]) / 20
            sma50 = sum(prices[-50:]) / 50
            daily_change = (price / prices[-2] - 1) * 100
            trend = price > sma20 and sma20 > sma50
            returns = [prices[i]/prices[i-1]-1 for i in range(-20, 0)]
            avg = sum(returns) / len(returns)
            volatility = (sum((r-avg)**2 for r in returns)/len(returns))**0.5 * 100
            test = backtest_trend(prices)
            out.append({'symbol':symbol,'name':name,'price':round(price,4),'sma20':round(sma20,4),
                        'sma50':round(sma50,4),'daily_change':round(daily_change,2),
                        'trend':trend,'date':latest_date.isoformat(),'source':source,'currency':'USD',
                        'signal':'Abwarten','reason':'Noch nicht bewertet','available':True, 'volatility_pct':round(volatility,2), 'backtest':test})
        except Exception:
            out.append({'symbol':symbol,'name':name,'available':False,'signal':'Keine Daten',
                        'reason':'Kursquelle nicht verfügbar oder Kurse veraltet'})
    return out

@app.post('/step')
def step():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    try:
        news, score = fetch_news()
        error = None
    except Exception:
        news, score, error = [], None, 'Nachrichten momentan nicht abrufbar – keine neuen Käufe'
    market = fetch_market()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    with LOCK:
        # Restore the paper ledger saved by GitHub Actions on a separate
        # private branch. Never restore broker credentials or place real orders.
        payload = request.get_json(silent=True)
        previous = payload.get('previous_state') if isinstance(payload, dict) else None
        if isinstance(previous, dict):
            try:
                restored = {
                    'cash': float(previous['cash']),
                    'shares': float(previous['shares']),
                    'price': float(previous['price']),
                    'peak': float(previous['peak']),
                    'tick': int(previous['tick']),
                    'halted': previous['halted'],
                    'symbol': previous.get('symbol'),
                    'trades': previous['trades'],
                    'last_trade_date': previous.get('last_trade_date'),
                    'entry_price': float(previous.get('entry_price') or 0),
                    'equity_history': previous.get('equity_history', []),
                    'last_step_at': previous.get('last_step_at')
                }
                valid = (
                    all(math.isfinite(restored[k]) and restored[k] >= 0
                        for k in ('cash', 'shares', 'price', 'peak', 'entry_price'))
                    and 0 <= restored['tick'] <= 10000000
                    and isinstance(restored['halted'], bool)
                    and isinstance(restored['trades'], list)
                    and len(restored['trades']) <= 50
                    and isinstance(restored['equity_history'], list)
                    and len(restored['equity_history']) <= 120
                    and (restored['symbol'] is None or
                         restored['symbol'] in [x[0] for x in WATCHLIST])
                    and (restored['shares'] == 0 or restored['symbol'] is not None)
                )
                if valid and restored['tick'] >= state['tick']:
                    state.update(restored)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        state['news_checked'] = now.strftime('%d.%m.%Y %H:%M UTC')
        state['news'], state['news_score'], state['news_error'] = news, score, error
        state['market_checked'] = now.strftime('%d.%m.%Y %H:%M UTC')
        state['market_error'] = None if any(m['available'] for m in market) else 'Keine aktuellen Marktdaten verfügbar – keine Trades'
        state['tick'] += 1
        state['last_step_at'] = now.isoformat()
        held = next((m for m in market if m['available'] and m['symbol'] == state['symbol']), None)
        # Keine Bewertung auf Basis erfundener oder veralteter Preise.
        if state['symbol'] and held:
            state['price'] = held['price']
        elif state['symbol']:
            state['market_error'] = 'Position kann wegen fehlender aktueller Kurse nicht neu bewertet werden'
        equity = state['cash'] + state['shares'] * state['price']
        state['peak'] = max(state['peak'], equity)
        for m in market:
            if not m['available']:
                continue
            if m['symbol'] == state['symbol'] and state['shares'] > 0:
                if m['price'] < m['sma20'] or score is not None and score <= -2:
                    m['signal'], m['reason'] = 'Virtuell verkaufen', 'Kurs unter 20-Tage-Durchschnitt oder negative Nachrichten'
                else:
                    m['signal'], m['reason'] = 'Halten', 'Position vorhanden; kein Verkaufssignal'
            elif m['trend'] and score is not None and score >= 0 and abs(m['daily_change']) <= 5:
                m['signal'], m['reason'] = 'Kaufkandidat', 'Kurs über 20-Tage-Linie, 20-Tage über 50-Tage, Nachrichten nicht negativ'
            else:
                m['signal'], m['reason'] = 'Abwarten', 'Trend, Tagesbewegung oder Nachrichtenfilter nicht erfüllt'
        state['market'] = market
        if state['halted']:
            state['last_decision'] = 'Simulation durch Risikogrenze gestoppt'
        elif state['shares'] > 0 and held is None:
            state['last_decision'] = 'Abwarten: kein aktueller Kurs für gehaltene Aktie'
        elif equity < state['peak'] * 0.95:
            state['halted'] = True
            state['last_decision'] = '5 % Rückgang vom Höchststand: Simulation angehalten (keine weiteren Trades)'
        elif state['shares'] > 0 and held and (held['signal'] == 'Virtuell verkaufen' or held['price'] <= state.get('entry_price', held['price']) * 0.97):
            if state['last_trade_date'] == today:
                state['last_decision'] = 'Abwarten: höchstens ein Trade pro UTC-Kalendertag'
            else:
                amount_usd = state['shares'] * state['price']
                # Der Simulator führt ein vereinfachtes USD-Buch; kein EUR/USD-Umrechnungskurs.
                state['cash'] += amount_usd
                state['trades'].append({'step':state['tick'],'side':'SIM_SELL','symbol':state['symbol'], 'price':round(state['price'],4),'amount':round(amount_usd,2),'reason':held['reason'] if held['signal'] == 'Virtuell verkaufen' else '3 % Verlustgrenze', 'currency':'USD'})
                state['shares'], state['symbol'], state['price'] = 0.0, None, 0.0
                state['last_trade_date'] = today
                state['last_decision'] = 'Virtueller Verkauf von ' + held['symbol']
        elif state['shares'] == 0 and state['last_trade_date'] != today and not state['market_error']:
            candidates = [m for m in market if m['available'] and m['signal'] == 'Kaufkandidat' and m.get('volatility_pct',100) <= 3.5]
            if candidates:
                chosen = sorted(candidates, key=lambda m: (m['price']/m['sma20']-1)/max(m['volatility_pct'],0.3), reverse=True)[0]
                spend = min(state['cash'], equity * 0.10)
                if spend > 0:
                    state['shares'] = spend / chosen['price']
                    state['cash'] -= spend
                    state['symbol'], state['price'], state['entry_price'] = chosen['symbol'], chosen['price'], chosen['price']
                    state['last_trade_date'] = today
                    state['trades'].append({'step':state['tick'],'side':'SIM_BUY','symbol':chosen['symbol'],'price':chosen['price'],'amount':round(spend,2),'reason':chosen['reason'],'currency':'USD'})
                    state['last_decision'] = 'Virtueller Kauf von ' + chosen['symbol'] + ': ' + chosen['reason']
            else:
                state['last_decision'] = 'Abwarten: kein Kaufkandidat mit ausreichenden Daten und Signalen'
        else:
            state['last_decision'] = 'Abwarten: Position halten oder Tageslimit erreicht'
        state['trades'] = state['trades'][-50:]
        equity_now = state['cash'] + state['shares'] * state['price']
        if not state['equity_history'] or state['equity_history'][-1].get('date') != today:
            state['equity_history'].append({'date': today, 'equity': round(equity_now, 2)})
        else:
            state['equity_history'][-1] = {'date': today, 'equity': round(equity_now, 2)}
        state['equity_history'] = state['equity_history'][-120:]
        return jsonify(snapshot())

@app.post('/stop')
def stop():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    with LOCK:
        state['halted'] = True
        return jsonify(snapshot())

PAGE = '''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#101827"><title>Mein Demo-Trading-Bot</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#f3f6fc;font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:600px;margin:auto;padding:28px 18px 70px}h1{font-size:27px;margin:6px 0 6px}.sub{color:#a7b4c9;font-size:14px;margin:0 0 26px}.card{background:#172238;border:1px solid #293650;border-radius:18px;padding:19px;margin-bottom:13px}.label{font-size:13px;color:#acbad0}.big{font-size:34px;font-weight:750;margin:8px 0 2px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.grid .card{min-width:0}.grid .big{font-size:23px}.positive{color:#7ee0aa}.negative{color:#ff9b9b}.pill{display:inline-block;border-radius:99px;background:#26354d;padding:7px 11px;font-size:13px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:12px 5px;text-align:left;border-bottom:1px solid #304059}th{color:#acbad0;font-weight:500}td:last-child,th:last-child{text-align:right}button{background:#3c83f6;color:white;border:0;border-radius:12px;padding:14px 17px;font-weight:650;font-size:16px;width:100%;cursor:pointer}input{background:#0e182b;border:1px solid #40506b;color:white;padding:14px;border-radius:12px;width:100%;font-size:16px;margin:12px 0}a{color:#a9c8ff}.foot{font-size:12px;color:#91a0b7;line-height:1.6;margin-top:24px}.news-item{padding:11px 0;border-bottom:1px solid #304059;font-size:14px;line-height:1.4}.news-item:last-child{border:0}.row{display:flex;align-items:center;justify-content:space-between;gap:12px}.logout{font-size:13px}.empty{color:#acbad0;font-size:14px}details.trade-list{margin-top:10px}summary.trade-toggle{list-style:none;background:#263c60;color:#f3f6fc;border-radius:12px;padding:15px;font-weight:700;cursor:pointer;text-align:center}summary.trade-toggle::-webkit-details-marker{display:none}summary.trade-toggle:after{content:"  ▾"}details[open] summary.trade-toggle:after{content:"  ▴"}.trade-content{margin-top:14px;overflow-x:auto}.trade-item{border-bottom:1px solid #304059;padding:13px 0}.trade-item:last-child{border:0}.trade-head{display:flex;justify-content:space-between;gap:8px;font-weight:650;margin-bottom:7px}.trade-meta{font-size:13px;color:#acbad0;line-height:1.65}.buy{color:#7ee0aa}.sell{color:#ffca86}
</style></head><body><main>{% if login %}<h1>🤖 Mein Demo-Trading-Bot</h1><p class="sub">Bitte mit deinem Dashboard-Passwort anmelden.</p><div class="card"><form method="post" action="{{ url_for('dashboard_login') }}"><label for="token">Dashboard-Passwort</label><input id="token" name="token" type="password" autocomplete="current-password" required><button type="submit">Anmelden</button></form>{% if error %}<p class="negative">Passwort falsch. Bitte erneut versuchen.</p>{% endif %}</div>{% else %}<div class="row"><h1>🤖 Mein Trading-Bot</h1><a class="logout" href="{{ url_for('dashboard_logout') }}">Abmelden</a></div><p class="sub">Virtuelle Simulation · Keine echten Aktienkäufe</p><div class="card"><div class="label">Virtueller Kontostand (USD)</div><div class="big">{{ '%.2f'|format(data.equity) }} USD</div><span class="pill">{{ '⛔ Gestoppt' if data.halted else '🟢 Simulation bereit' }}</span></div><div class="grid"><div class="card"><div class="label">Gewinn / Verlust (USD)</div><div class="big {{ 'positive' if gain >= 0 else 'negative' }}">{{ '%+.2f'|format(gain) }} USD</div></div><div class="card"><div class="label">Simulationsschritte</div><div class="big">{{ data.tick }}</div></div><div class="card"><div class="label">Verfügbares Geld (USD)</div><div class="big">{{ '%.2f'|format(data.cash) }} USD</div></div><div class="card"><div class="label">Virtuelle Position (USD)</div><div class="big">{{ '%.2f'|format(data.shares * data.price) }} USD</div></div></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">Käufe und Verkäufe</h2><p class="empty">{{ data.trades|length }} gespeicherte simulierte Trades</p><details class="trade-list"><summary class="trade-toggle">📋 Käufe &amp; Verkäufe anzeigen</summary><div class="trade-content">{% if data.trades %}{% for trade in data.trades|reverse %}<div class="trade-item"><div class="trade-head"><span class="{{ 'buy' if trade.side == 'SIM_BUY' else 'sell' }}">{{ '🟢 Kauf' if trade.side == 'SIM_BUY' else '🟠 Verkauf' }}</span><span>{{ '%.2f'|format(trade.amount) }} USD</span></div><div class="trade-meta">Aktie: {{ trade.symbol|default('Demo') }}<br>Grund: {{ trade.reason|default('Frühere Demo-Regel') }}<br>Simulationsschritt: {{ trade.step }}<br>Preis pro Anteil: {{ '%.4f'|format(trade.price) }} USD<br>Anteile: {{ '%.5f'|format(trade.amount / trade.price) if trade.price else '—' }}</div></div>{% endfor %}{% else %}<p class="empty">Noch keine simulierten Käufe oder Verkäufe. Käufe nur bei gültigen Marktdaten und passenden Signalen.</p>{% endif %}</div></details></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">📈 Echte historische Aktienkurse</h2><p class="empty">Letzte Prüfung: {{ data.market_checked or 'Noch keine Prüfung' }} · Tages-Schlusskurse in USD, keine Echtzeitdaten</p>{% if data.market_error %}<p class="negative">{{ data.market_error }}</p>{% endif %}<details class="trade-list" open><summary class="trade-toggle">📊 Aktien &amp; Signale anzeigen</summary><div class="trade-content">{% for m in data.market %}<div class="trade-item"><div class="trade-head"><span>{{ m.name }} ({{ m.symbol }})</span><span>{{ ('%.2f'|format(m.price) ~ ' USD') if m.available else '—' }}</span></div><div class="trade-meta"><strong>{{ m.signal }}</strong> · {{ m.reason }}{% if m.available %}<br>Datum: {{ m.date }} · Quelle: {{ m.source }}<br>20-Tage-Mittel: {{ '%.2f'|format(m.sma20) }} · 50-Tage-Mittel: {{ '%.2f'|format(m.sma50) }} USD<br>Letzter Tageswechsel: {{ '%+.2f'|format(m.daily_change) }} %{% endif %}</div></div>{% else %}<p class="empty">Noch keine Kursprüfung. Daten erscheinen nach dem nächsten automatischen Simulationsschritt.</p>{% endfor %}</div></details></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">📰 Nachrichten &amp; Entscheidungen</h2><p class="empty">Letzte Prüfung: {{ data.news_checked or 'Noch nicht geprüft' }}</p><p><strong>Letzte Entscheidung:</strong> {{ data.last_decision }}</p>{% if data.news_error %}<p class="negative">{{ data.news_error }}</p>{% endif %}<p class="empty">Nachrichten-Score: {{ data.news_score if data.news_score is not none else 'Nicht verfügbar' }} · Schlagwort-Heuristik, keine verlässliche Marktprognose</p><details class="trade-list"><summary class="trade-toggle">📰 Nachrichten anzeigen</summary><div class="trade-content">{% if data.news %}{% for item in data.news %}<div class="news-item">{% if item.url %}<a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}<br><span class="empty">{{ item.published }}</span></div>{% endfor %}{% else %}<p class="empty">Noch keine aktuellen Nachrichten gespeichert.</p>{% endif %}</div></details></div><button type="button" onclick="location.reload()">🔄 Werte aktualisieren</button><p class="foot">Startkapital: 1.000 USD virtuell (keine EUR-Umrechnung) · Historische USD-Tageskurse, keine Trading-212-Verbindung. Nachrichten und Trendlinien sind einfache Demo-Heuristiken, keine realen Aktienempfehlungen. Keine Gebühren, Spreads, Steuern oder Wechselkurse berücksichtigt. Kostenlose Kursquellen können ausfallen. Die Anzeige beweist nicht, dass GitHub regelmäßig startet. Der virtuelle Kontostand wird bei erfolgreichen GitHub-Actions-Läufen im privaten Speicherzweig gesichert. Nach einem Render-Neustart kann die Anzeige bis zum nächsten erfolgreichen Lauf vorübergehend Startwerte zeigen.</p>{% endif %}</main></body></html>'''

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
