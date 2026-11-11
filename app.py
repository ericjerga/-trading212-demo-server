"""Synthetic-data demo trading simulator with a password-protected mobile dashboard.
No broker orders. USD-denominated paper ledger, no FX conversion. The paper ledger is restored from GitHub Actions on scheduled steps and, with a read-only GitHub token, when opening the dashboard.
"""
import os
import math
import random
import urllib.request
import urllib.error
import json
import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import secrets
import re
import threading
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
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
CRYPTO_START = 200.0
state = {'cash': START, 'shares': 0.0, 'price': 0.0, 'symbol': None, 'tick': 0, 'trades': [], 'halted': False, 'peak': START, 'news': [], 'news_checked': None, 'news_error': None, 'last_decision': 'Noch keine Prüfung', 'news_score': None, 'market': [], 'market_checked': None, 'market_error': None, 'last_trade_date': None, 'equity_history': [], 'last_step_at': None, 'crypto': {'cash': CRYPTO_START, 'shares': 0.0, 'price': 0.0, 'symbol': None, 'entry_price': 0.0, 'peak': CRYPTO_START, 'halted': False, 'trades': [], 'last_trade_date': None, 'last_decision': 'Noch keine Prüfung', 'market': [], 'checked': None, 'error': None, 'news': [], 'news_score': None, 'news_error': None}}

def valid_token(value):
    token = os.environ.get('DASHBOARD_TOKEN', '')
    return bool(token) and secrets.compare_digest(value, token)

def authorized():
    return valid_token(request.headers.get('X-Dashboard-Token', ''))

# Optional read-only GitHub token lets a sleeping Render instance recover its
# private persisted ledger before displaying the dashboard. Never expose it in HTML.
GITHUB_STATE_URL = 'https://api.github.com/repos/ericjerga/-trading212-demo-server/contents/state.json?ref=bot-state'
RESTORE_LOCK = threading.Lock()

def restore_from_github():
    token = os.environ.get('GITHUB_STATE_TOKEN', '').strip()
    if not token:
        return False
    # Avoid concurrent requests overwriting newer in-memory simulation results.
    with RESTORE_LOCK:
        with LOCK:
            current_tick = state['tick']
        req = urllib.request.Request(GITHUB_STATE_URL, headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github.raw+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'PaperTradingDemo/1.0',
        })
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                saved = json.loads(resp.read(200000))
            crypto = saved.get('crypto')
            tick = saved.get('tick')
            if (not isinstance(tick, int) or tick < 1 or tick > 10000000
                or not isinstance(crypto, dict)
                or not isinstance(saved.get('trades'), list)
                or not isinstance(crypto.get('trades'), list)
                or not isinstance(saved.get('equity_history'), list)
                or len(saved['trades']) > 50 or len(crypto['trades']) > 50
                or len(saved['equity_history']) > 120):
                return False
            for key in ('cash','shares','price','peak'):
                value = saved.get(key)
                if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0:
                    return False
            for key in ('cash','shares','price','peak'):
                value = crypto.get(key)
                if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0:
                    return False
            if saved.get('symbol') is not None and saved['symbol'] not in [x[0] for x in WATCHLIST]:
                return False
            if saved['shares'] and not saved.get('symbol'):
                return False
            if crypto.get('symbol') is not None and crypto['symbol'] not in CRYPTO_COINS:
                return False
            if crypto['shares'] and not crypto.get('symbol'):
                return False
            allowed = ('cash','shares','price','peak','tick','halted','symbol',
                       'trades','last_trade_date','entry_price','equity_history',
                       'last_step_at','crypto')
            with LOCK:
                if tick > state['tick'] and tick > current_tick:
                    state.update({key: saved[key] for key in allowed if key in saved})
                    state['last_decision'] = 'Gespeicherter Simulationsstand aus GitHub wiederhergestellt; nächste Prüfung steht aus'
                    return True
        except (urllib.error.URLError, ValueError, TypeError, KeyError, TimeoutError):
            pass
    return False

def snapshot():
    crypto = state['crypto']
    crypto_equity = crypto['cash'] + crypto['shares'] * crypto['price']
    equity = state['cash'] + state['shares'] * state['price']
    return {**state, 'crypto_equity': round(crypto_equity, 2), 'equity': round(equity, 2), 'cash': round(state['cash'], 2), 'price': round(state['price'], 4), 'shares': round(state['shares'], 5), 'environment': 'historical_market_data_paper_trading', 'real_orders_enabled': False, 'trading212_connected': False}

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
    with LOCK:
        last = state.get('last_step_at')
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
        except (TypeError, ValueError):
            age = None
        fresh = age is not None and 0 <= age <= 2700
        return jsonify(status='ok' if fresh else 'waiting_or_stale',
                       last_step_at=last, simulation_recent=fresh,
                       real_orders_enabled=False,
                       environment='historical_market_data_paper_trading')

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
    def one_stock(item):
        symbol, name = item
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
            return {'symbol':symbol,'name':name,'price':round(price,4),'sma20':round(sma20,4),
                        'sma50':round(sma50,4),'daily_change':round(daily_change,2),
                        'trend':trend,'date':latest_date.isoformat(),'source':source,'currency':'USD',
                        'signal':'Abwarten','reason':'Noch nicht bewertet','available':True, 'volatility_pct':round(volatility,2), 'backtest':test}
        except Exception:
            return {'symbol':symbol,'name':name,'available':False,'signal':'Keine Daten',
                    'reason':'Kursquelle nicht verfügbar oder Kurse veraltet'}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one_stock, WATCHLIST))

def us_stock_session(now):
    ny = now.astimezone(ZoneInfo('America/New_York'))
    # Conservative: no stock paper orders on weekends or full US market holidays.
    from datetime import date, time
    d = ny.date()
    if d.weekday() >= 5:
        return False
    def observed(y, m, day):
        x = date(y, m, day)
        return x + timedelta(days=1) if x.weekday() == 6 else x - timedelta(days=1) if x.weekday() == 5 else x
    def nth(y, m, wd, n):
        x = date(y, m, 1)
        return x + timedelta(days=(wd-x.weekday())%7+7*(n-1))
    def last_monday(y, m):
        x = date(y, m+1, 1)-timedelta(days=1)
        return x-timedelta(days=x.weekday())
    def easter(y):
        a=y%19; b=y//100; c=y%100; d=b//4; e=b%4
        f=(b+8)//25; g=(b-f+1)//3; h=(19*a+b-d-g+15)%30
        i=c//4; k=c%4; l=(32+2*e+2*i-h-k)%7
        m=(a+11*h+22*l)//451; month=(h+l-7*m+114)//31
        return date(y,month,(h+l-7*m+114)%31+1)
    holidays = set()
    for y in (d.year-1,d.year,d.year+1):
        holidays.update((observed(y,1,1),nth(y,1,0,3),nth(y,2,0,3),
                         easter(y)-timedelta(days=2),last_monday(y,5),
                         observed(y,6,19),observed(y,7,4),nth(y,9,0,1),
                         nth(y,11,3,4),observed(y,12,25)))
    if d in holidays:
        return False
    early = d == nth(d.year,11,3,4)+timedelta(days=1) or (d.month,d.day) in ((12,24),(7,3))
    return time(9,30) <= ny.time().replace(tzinfo=None) < (time(13) if early else time(16))


CRYPTO_COINS = {
 'BTC': ('bitcoin','BTC-USD',('bitcoin','btc')),
 'ETH': ('ethereum','ETH-USD',('ethereum','ether','eth')),
 'SOL': ('solana','SOL-USD',('solana','sol')),
 'XRP': ('ripple','XRP-USD',('xrp','ripple')),
 'ADA': ('cardano','ADA-USD',('cardano','ada')),
 'LINK': ('chainlink','LINK-USD',('chainlink','link')),
 'AVAX': ('avalanche-2','AVAX-USD',('avalanche','avax')),
 'DOGE': ('dogecoin','DOGE-USD',('dogecoin','doge')),
}

def _safe_crypto_error(exc):
    if isinstance(exc, urllib.error.HTTPError): return 'HTTP '+str(exc.code)
    if isinstance(exc, urllib.error.URLError): return 'Netzwerk-/DNS-/TLS-Fehler'
    if isinstance(exc, TimeoutError): return 'Zeitüberschreitung'
    return 'Ungültige/veraltete Kursdaten ('+type(exc).__name__+')'

def _fetch_coingecko_crypto():
    ids=','.join(v[0] for v in CRYPTO_COINS.values())
    url=('https://api.coingecko.com/api/v3/simple/price?ids='+ids+
         '&vs_currencies=usd&include_24hr_change=true&include_last_updated_at=true')
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 PaperTradingDemo/1.0','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=9) as resp: data=json.load(resp)
    now=datetime.now(timezone.utc).timestamp()
    result={}
    for symbol,(coin_id,_,_) in CRYPTO_COINS.items():
        try:
            item=data.get(coin_id,{})
            price=float(item.get('usd',0))
            change=float(item.get('usd_24h_change',float('nan')))
            updated=float(item.get('last_updated_at',0))
            if not all(math.isfinite(x) for x in (price,change,updated)) or price<=0 or not 0<=now-updated<=1800: continue
            result[symbol]={'symbol':symbol,'price':price,'change_24h':round(change,2),'source':'CoinGecko'}
        except (TypeError,ValueError,OverflowError): continue
    return result

def _fetch_coinbase_coin(symbol):
    base='https://api.exchange.coinbase.com/products/'+CRYPTO_COINS[symbol][1]+'/'
    headers={'User-Agent':'Mozilla/5.0 PaperTradingDemo/1.0','Accept':'application/json'}
    def get(path):
        req=urllib.request.Request(base+path,headers=headers)
        with urllib.request.urlopen(req,timeout=8) as resp: return json.load(resp)
    ticker=get('ticker')
    stats=get('stats')
    price=float(ticker.get('price',0))
    opening=float(stats.get('open',0))
    observed=datetime.fromisoformat(str(ticker.get('time','')).replace('Z','+00:00'))
    age=(datetime.now(timezone.utc)-observed).total_seconds()
    if not all(math.isfinite(x) for x in (price,opening)) or price<=0 or opening<=0 or not 0<=age<=900:
        raise ValueError('Coinbase-Kurs fehlt oder ist veraltet')
    return {'symbol':symbol,'price':price,'change_24h':round((price/opening-1)*100,2),'source':'Coinbase Exchange'}

def fetch_crypto_market():
    rows={}
    errors=[]
    try: rows=_fetch_coingecko_crypto()
    except Exception as exc: errors.append('CoinGecko: '+_safe_crypto_error(exc))
    missing=[symbol for symbol in CRYPTO_COINS if symbol not in rows]
    def fallback(symbol):
        try: return symbol,_fetch_coinbase_coin(symbol),None
        except Exception as exc: return symbol,None,_safe_crypto_error(exc)
    if missing:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for symbol,row,error in pool.map(fallback,missing):
                if row: rows[symbol]=row
                else: errors.append(symbol+': '+error)
    if not rows: raise ValueError('Keine gültigen Kryptokurse. '+'; '.join(errors[:3]))
    if len(rows) != len(CRYPTO_COINS):
        raise ValueError('Unvollständige Kryptokurse: %d von %d. %s' % (len(rows), len(CRYPTO_COINS), '; '.join(errors[:3])))
    return [rows[symbol] for symbol in CRYPTO_COINS if symbol in rows]

CRYPTO_RSS_URL = ('https://news.google.com/rss/search?q=Bitcoin+OR+Ethereum+OR+Solana+OR+XRP+OR+Cardano+OR+Chainlink+OR+Avalanche+OR+Dogecoin+when%3A1d&hl=de&gl=DE&ceid=DE:de')
CRYPTO_NEGATIVE = ('hack', 'gehackt', 'diebstahl', 'betrug', 'scam', 'pleite', 'insolvenz', 'verbot', 'verboten', 'klage', 'crash', 'einbruch', 'verlust', 'liquidation', 'sanktion', 'exploit', 'breach', 'fraud', 'ban', 'lawsuit')
CRYPTO_POSITIVE = ('zulassung', 'genehmigung', 'etf-zufluss', 'zuflüsse', 'adoption', 'akzeptanz', 'erholung', 'approval', 'inflows', 'recovery')

def fetch_crypto_news():
    req=urllib.request.Request(CRYPTO_RSS_URL,headers={'User-Agent':'Mozilla/5.0 DemoResearchBot/1.0','Accept':'application/rss+xml'})
    with urllib.request.urlopen(req,timeout=9) as resp: root=ET.fromstring(resp.read(250000))
    now=datetime.now(timezone.utc)
    items=[]
    seen=set()
    per_coin={symbol:{'score':None,'count':0} for symbol in CRYPTO_COINS}
    scores={symbol:0 for symbol in CRYPTO_COINS}
    for item in root.findall('./channel/item')[:80]:
        title=(item.findtext('title') or '').strip()
        link=(item.findtext('link') or '').strip()
        try: published=parsedate_to_datetime(item.findtext('pubDate') or '').astimezone(timezone.utc)
        except (ValueError,TypeError,OverflowError): continue
        if not title or title.casefold() in seen or published>now+timedelta(minutes=5) or now-published>timedelta(hours=24): continue
        seen.add(title.casefold())
        lower=title.casefold()
        score=sum(term in lower for term in CRYPTO_POSITIVE)-2*sum(term in lower for term in CRYPTO_NEGATIVE)
        matched=[symbol for symbol,(_,_,aliases) in CRYPTO_COINS.items()
                 if any(re.search(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',lower) for alias in aliases)]
        for symbol in matched:
            scores[symbol]+=score
            per_coin[symbol]['count']+=1
        items.append({'title':title[:180],'url':link if link.startswith('https://') else '',
                      'published':published.strftime('%d.%m. %H:%M UTC'),'score':score,'coins':matched})
        if len(items)>=40: break
    for symbol in CRYPTO_COINS:
        if per_coin[symbol]['count']: per_coin[symbol]['score']=scores[symbol]
    if not items: raise ValueError('Keine aktuellen Kryptomeldungen')
    return items,per_coin


def crypto_trade_cooldown_active(crypto, now):
    last = crypto.get('last_trade_at')
    if last:
        try:
            parsed = datetime.fromisoformat(last)
            if parsed.tzinfo is not None:
                age = (now - parsed).total_seconds()
                return age < 4 * 3600
        except (TypeError, ValueError, OverflowError):
            pass
    # Older persisted states have only a date. Retain the old daily limit
    # until the first new trade creates a precise timestamp.
    return crypto.get('last_trade_date') == now.date().isoformat()


def simulate_crypto(now,market,error,news,news_score,news_error):
    c=state['crypto']; today=now.date().isoformat()
    c['news'],c['news_by_coin'],c['news_error']=news,news_score,news_error
    c['news_score']=None
    c['market'],c['checked'],c['error']=market,now.isoformat(),error
    held=next((m for m in market if m['symbol']==c['symbol']),None)
    if held: c['price']=held['price']
    equity=c['cash']+c['shares']*c['price']
    c['peak']=max(c['peak'],equity)
    if error or (c['shares'] and not held):
        c['last_decision']='Warten: aktuelle Kryptokurse fehlen'; return
    if equity<c['peak']*0.95: c['halted']=True
    if c['halted']:
        c['last_decision']='Krypto-Risikogrenze erreicht'
    elif crypto_trade_cooldown_active(c, now):
        c['last_decision']='Krypto-Cooldown: mindestens vier Stunden zwischen Trades'
    elif held and (held['price']<=c['entry_price']*0.97 or held['change_24h'] < -1):
        amount=c['shares']*held['price']; c['cash']+=amount
        c['trades'].append({'side':'SIM_SELL','symbol':c['symbol'],'price':round(held['price'],4),'amount':round(amount,2),'date':today})
        c['shares'],c['symbol'],c['price'],c['entry_price']=0.0,None,0.0,0.0
        c['last_trade_date']=today; c['last_trade_at']=now.isoformat(); c['last_decision']='Virtueller Krypto-Verkauf'
    elif c['shares']==0:
        candidates=[m for m in market if 1<=m['change_24h']<=5
                    and not news_error and isinstance(news_score,dict)
                    and news_score.get(m['symbol'],{}).get('score') is not None
                    and news_score[m['symbol']]['score'] >= 0]
        if candidates:
            chosen=max(candidates,key=lambda m:m['change_24h'])
            spend=min(c['cash'],equity*0.10)
            if spend>0:
                c['cash']-=spend; c['shares']=spend/chosen['price']
                c['symbol'],c['price'],c['entry_price']=chosen['symbol'],chosen['price'],chosen['price']
                c['last_trade_date']=today; c['last_trade_at']=now.isoformat()
                c['trades'].append({'side':'SIM_BUY','symbol':chosen['symbol'],'price':round(chosen['price'],4),'amount':round(spend,2),'date':today})
                c['last_decision']='Virtueller Kauf '+chosen['symbol']
        else: c['last_decision']='Kein Kaufsignal mit ausreichenden, nicht-negativen Coin-Nachrichten'
    else: c['last_decision']='Krypto-Position halten'
    c['trades']=c['trades'][-50:]


@app.post('/step')
def step():
    if not authorized(): return jsonify(error='Unauthorized'), 401
    # Independent remote feeds run concurrently to reduce step latency.
    # Each fetch retains its own timeout and fail-closed handling.
    with ThreadPoolExecutor(max_workers=4) as pool:
        news_future = pool.submit(fetch_news)
        market_future = pool.submit(fetch_market)
        crypto_future = pool.submit(fetch_crypto_market)
        crypto_news_future = pool.submit(fetch_crypto_news)
        try:
            news, score = news_future.result()
            error = None
        except Exception:
            news, score, error = [], None, 'Nachrichten momentan nicht abrufbar – keine neuen Käufe'
        market = market_future.result()
        try:
            crypto_market, crypto_error = crypto_future.result(), None
        except Exception as exc:
            crypto_market, crypto_error = [], 'Kryptokurse nicht verfügbar – kein Handel. Diagnose: ' + str(exc)[:180]
        try:
            crypto_news, crypto_news_score = crypto_news_future.result()
            crypto_news_error = None
        except Exception:
            crypto_news, crypto_news_score = [], None
            crypto_news_error = 'Keine ausreichend aktuellen Kryptonachrichten – keine neuen Käufe'
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
                    'last_step_at': previous.get('last_step_at'),
                    'crypto': previous.get('crypto', state['crypto'])
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
                    and isinstance(restored['crypto'], dict)
                    and isinstance(restored['crypto'].get('trades'), list)
                    and len(restored['crypto']['trades']) <= 50
                    and (restored['symbol'] is None or
                         restored['symbol'] in [x[0] for x in WATCHLIST])
                    and (restored['shares'] == 0 or restored['symbol'] is not None)
                )
                if valid and restored['tick'] >= state['tick']:
                    state.update(restored)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        simulate_crypto(now, crypto_market, crypto_error, crypto_news, crypto_news_score, crypto_news_error)
        state['news_checked'] = now.strftime('%d.%m.%Y %H:%M UTC')
        state['news'], state['news_score'], state['news_error'] = news, score, error
        state['market_checked'] = now.strftime('%d.%m.%Y %H:%M UTC')
        available_count = sum(bool(m['available']) for m in market)
        state['market_error'] = None if available_count >= 3 else 'Zu wenige aktuelle Marktdaten (mindestens 3 erforderlich) – keine neuen Käufe'
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
        if not us_stock_session(now):
            state['last_decision'] = 'US-Aktienbörse geschlossen: keine virtuellen Aktien-Trades'
        elif state['halted']:
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
</style></head><body><main>{% if login %}<h1>🤖 Mein Demo-Trading-Bot</h1><p class="sub">Bitte mit deinem Dashboard-Passwort anmelden.</p><div class="card"><form method="post" action="{{ url_for('dashboard_login') }}"><label for="token">Dashboard-Passwort</label><input id="token" name="token" type="password" autocomplete="current-password" required><button type="submit">Anmelden</button></form>{% if error %}<p class="negative">Passwort falsch. Bitte erneut versuchen.</p>{% endif %}</div>{% else %}<div class="row"><h1>🤖 Mein Trading-Bot</h1><a class="logout" href="{{ url_for('dashboard_logout') }}">Abmelden</a></div><p class="sub">Virtuelle Simulation · Keine echten Aktienkäufe</p>{% if not data.last_step_at %}<p class="negative">⚠️ Noch kein erfolgreicher Simulationsschritt seit dem letzten Serverstart.</p>{% endif %}{% if data.market_error %}<p class="negative">⚠️ {{ data.market_error }}</p>{% endif %}<div class="card"><div class="label">Virtueller Kontostand (USD)</div><div class="big">{{ '%.2f'|format(data.equity) }} USD</div><span class="pill">{{ '⛔ Gestoppt' if data.halted else '🟢 Simulation bereit' }}</span></div><div class="grid"><div class="card"><div class="label">Gewinn / Verlust (USD)</div><div class="big {{ 'positive' if gain >= 0 else 'negative' }}">{{ '%+.2f'|format(gain) }} USD</div></div><div class="card"><div class="label">Simulationsschritte</div><div class="big">{{ data.tick }}</div></div><div class="card"><div class="label">Verfügbares Geld (USD)</div><div class="big">{{ '%.2f'|format(data.cash) }} USD</div></div><div class="card"><div class="label">Virtuelle Position (USD)</div><div class="big">{{ '%.2f'|format(data.shares * data.price) }} USD</div></div></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">₿ Krypto-Simulation (separat)</h2><div class="big">{{ '%.2f'|format(data.crypto_equity) }} USD</div><p class="empty">Start: 200 virtuelle USD · 8 Kryptowährungen · 24/7</p><p><strong>Entscheidung:</strong> {{ data.crypto.last_decision }}</p>{% if data.crypto.error %}<p class="negative">⚠️ {{ data.crypto.error }}</p>{% endif %}{% for coin in data.crypto.market %}<p class="empty">{{ coin.symbol }}: {{ '%.2f'|format(coin.price) }} USD · 24h: {{ '%+.2f'|format(coin.change_24h) }} %</p>{% endfor %}<p class="empty">Nachrichtenbewertung je Coin (einfache Schlagwortanalyse)</p>{% for symbol, info in (data.crypto.news_by_coin or {}).items() %}<p class="empty">{{ symbol }}: {{ info.score if info.score is not none else 'Keine Meldungen' }} · {{ info.count }} Berichte</p>{% endfor %}<p class="empty">Nur aktuelle Coin-Meldungen werden für neue Käufe berücksichtigt</p>{% if data.crypto.news_error %}<p class="negative">⚠️ {{ data.crypto.news_error }}</p>{% endif %}<details class="trade-list"><summary class="trade-toggle">📰 Aktuelle Kryptoberichte anzeigen</summary>{% for item in data.crypto.news %}<div class="news-item">{% if item.url %}<a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}<br><span class="empty">{{ item.published }} · {{ item.coins|join(', ') if item.coins else 'Allgemein' }} · Bewertung {{ item.score }}</span></div>{% else %}<p class="empty">Keine aktuellen Kryptoberichte verfügbar.</p>{% endfor %}</details><details class="trade-list"><summary class="trade-toggle">Krypto-Käufe und Verkäufe anzeigen</summary>{% for trade in data.crypto.trades|reverse %}<div class="trade-item">{{ trade.date }} · {{ trade.side }} {{ trade.symbol }} · {{ '%.2f'|format(trade.amount) }} USD</div>{% else %}<p class="empty">Noch keine virtuellen Krypto-Trades.</p>{% endfor %}</details></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">Käufe und Verkäufe</h2><p class="empty">{{ data.trades|length }} gespeicherte simulierte Trades</p><details class="trade-list"><summary class="trade-toggle">📋 Käufe &amp; Verkäufe anzeigen</summary><div class="trade-content">{% if data.trades %}{% for trade in data.trades|reverse %}<div class="trade-item"><div class="trade-head"><span class="{{ 'buy' if trade.side == 'SIM_BUY' else 'sell' }}">{{ '🟢 Kauf' if trade.side == 'SIM_BUY' else '🟠 Verkauf' }}</span><span>{{ '%.2f'|format(trade.amount) }} USD</span></div><div class="trade-meta">Aktie: {{ trade.symbol|default('Demo') }}<br>Grund: {{ trade.reason|default('Frühere Demo-Regel') }}<br>Simulationsschritt: {{ trade.step }}<br>Preis pro Anteil: {{ '%.4f'|format(trade.price) }} USD<br>Anteile: {{ '%.5f'|format(trade.amount / trade.price) if trade.price else '—' }}</div></div>{% endfor %}{% else %}<p class="empty">Noch keine simulierten Käufe oder Verkäufe. Käufe nur bei gültigen Marktdaten und passenden Signalen.</p>{% endif %}</div></details></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">📉 Virtueller Kontostand im Zeitverlauf</h2><p class="empty">{{ data.equity_history|length }} gespeicherte Tage · Letzte Prüfung: {{ data.last_step_at or 'Noch keine' }}</p>{% if data.equity_history|length > 1 %}<svg viewBox="0 0 320 115" role="img" aria-label="Kontostand-Verlauf" style="width:100%;height:auto"><polyline fill="none" stroke="#7ee0aa" stroke-width="2.5" stroke-linejoin="round" points="{% set vals = data.equity_history|map(attribute='equity')|list %}{% set low = vals|min %}{% set span = (vals|max - low) or 1 %}{% for h in data.equity_history %}{{ (10 + loop.index0 * 300 / (loop.length-1))|round(1) }},{{ (105 - (h.equity-low)*95/span)|round(1) }} {% endfor %}"/></svg>{% else %}<p class="empty">Der Verlauf erscheint nach zwei gespeicherten Tagen.</p>{% endif %}</div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">📈 Echte historische Aktienkurse</h2><p class="empty">Letzte Prüfung: {{ data.market_checked or 'Noch keine Prüfung' }} · Tages-Schlusskurse in USD, keine Echtzeitdaten</p>{% if data.market_error %}<p class="negative">{{ data.market_error }}</p>{% endif %}<details class="trade-list" open><summary class="trade-toggle">📊 Aktien &amp; Signale anzeigen</summary><div class="trade-content">{% for m in data.market %}<div class="trade-item"><div class="trade-head"><span>{{ m.name }} ({{ m.symbol }})</span><span>{{ ('%.2f'|format(m.price) ~ ' USD') if m.available else '—' }}</span></div><div class="trade-meta"><strong>{{ m.signal }}</strong> · {{ m.reason }}{% if m.available %}<br>Datum: {{ m.date }} · Quelle: {{ m.source }}<br>20-Tage-Mittel: {{ '%.2f'|format(m.sma20) }} · 50-Tage-Mittel: {{ '%.2f'|format(m.sma50) }} USD<br>Letzter Tageswechsel: {{ '%+.2f'|format(m.daily_change) }} %<br>Schwankung (20 Tage): {{ '%.2f'|format(m.volatility_pct) }} %{% if m.backtest %}<br>Historischer Strategietest: {{ '%+.2f'|format(m.backtest.return_pct) }} % · Kaufen &amp; Halten: {{ '%+.2f'|format(m.backtest.buy_hold_pct) }} % (ohne Gebühren){% endif %}{% endif %}</div></div>{% else %}<p class="empty">Noch keine Kursprüfung. Daten erscheinen nach dem nächsten automatischen Simulationsschritt.</p>{% endfor %}</div></details></div><div class="card"><h2 style="font-size:18px;margin:0 0 12px">📰 Nachrichten &amp; Entscheidungen</h2><p class="empty">Letzte Prüfung: {{ data.news_checked or 'Noch nicht geprüft' }}</p><p><strong>Letzte Entscheidung:</strong> {{ data.last_decision }}</p>{% if data.news_error %}<p class="negative">{{ data.news_error }}</p>{% endif %}<p class="empty">Nachrichten-Score: {{ data.news_score if data.news_score is not none else 'Nicht verfügbar' }} · Schlagwort-Heuristik, keine verlässliche Marktprognose</p><details class="trade-list"><summary class="trade-toggle">📰 Nachrichten anzeigen</summary><div class="trade-content">{% if data.news %}{% for item in data.news %}<div class="news-item">{% if item.url %}<a href="{{ item.url }}" target="_blank" rel="noopener noreferrer">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}<br><span class="empty">{{ item.published }}</span></div>{% endfor %}{% else %}<p class="empty">Noch keine aktuellen Nachrichten gespeichert.</p>{% endif %}</div></details></div><button type="button" onclick="location.reload()">🔄 Werte aktualisieren</button><p class="foot">Aktien-Startkapital: 1.000 USD virtuell · Krypto-Startkapital: getrennte 200 USD virtuell (keine EUR-Umrechnung) · Historische USD-Tageskurse, keine Trading-212-Verbindung. Nachrichten, Trendlinien und historische Tests sind einfache Demo-Heuristiken, keine realen Aktienempfehlungen. Keine Gebühren, Spreads, Steuern oder Wechselkurse berücksichtigt. Kostenlose Kursquellen können ausfallen. Die Anzeige beweist nicht, dass GitHub regelmäßig startet. Der virtuelle Kontostand wird bei erfolgreichen GitHub-Actions-Läufen im privaten Speicherzweig gesichert. Nach einem Render-Neustart wird der private GitHub-Speicher bei eingerichtetem GITHUB_STATE_TOKEN sofort gelesen; ohne dieses Token kann die Anzeige bis zum nächsten erfolgreichen Lauf Startwerte zeigen.</p>{% endif %}</main></body></html>'''

@app.get('/dashboard')
def dashboard():
    if not session.get('dashboard_logged_in'):
        return render_template_string(PAGE, login=True, error=False)
    restore_from_github()
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
