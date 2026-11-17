"""Research-only daily-bar PAPER strategy. Never imported by the live simulator."""
from dataclasses import dataclass
from math import sqrt, isfinite
from statistics import mean, pstdev

FEE_BPS = 15
SLIPPAGE_BPS = 15
MAX_EXPOSURE = 0.65

@dataclass
class Result:
    return_pct: float
    max_drawdown_pct: float
    trades: int
    sharpe: float
    buy_hold_pct: float

def backtest(prices, fee_bps=FEE_BPS, slippage_bps=SLIPPAGE_BPS,
             fast=10, slow=30, stop_pct=0.035, trail_pct=0.045,
             start=60, end=None):
    """Signal at close t-1; trade at close t (conservative daily proxy).
    Charge fees and slippage on BOTH entry and exit. End liquidation is charged.
    The stop is checked at closes, not an intraday guaranteed stop.
    """
    if len(prices)<100 or any(not isfinite(p) or p<=0 for p in prices):
        raise ValueError("At least 100 finite positive closes required")
    if not (0<=fee_bps<=200 and 0<=slippage_bps<=200 and 3<=fast<slow<=60):
        raise ValueError("Invalid strategy or cost parameters")
    end = len(prices)-1 if end is None else end
    if not (max(start,slow+11)<end<=len(prices)-1):
        raise ValueError("Invalid evaluation window")
    cost=(fee_bps+slippage_bps)/10000
    cash,shares,entry,high,trades=1.0,0.0,0.0,0.0,0
    curve=[1.0]
    for t in range(start,end+1):
        prev=prices[t-1]
        fast_sma=mean(prices[t-fast:t])
        slow_sma=mean(prices[t-slow:t])
        momentum=prev/prices[t-11]-1
        px=prices[t]
        if shares:
            high=max(high,prev)
            if prev<=entry*(1-stop_pct) or prev<=high*(1-trail_pct) or prev<fast_sma:
                cash+=shares*px*(1-cost)
                shares=0.0
                trades+=1
        elif prev>fast_sma>slow_sma and momentum>0.01:
            equity=cash+shares*px
            spend=min(cash,equity*MAX_EXPOSURE)
            if spend>0:
                cash-=spend
                shares=spend*(1-cost)/px
                entry=high=px
                trades+=1
        curve.append(cash+shares*px)
    if shares:
        cash+=shares*prices[end]*(1-cost)
        shares=0.0
        trades+=1
        curve[-1]=cash
    peaks=[curve[0]]
    for v in curve[1:]: peaks.append(max(peaks[-1],v))
    dd=max(1-v/p for v,p in zip(curve,peaks))
    rets=[curve[i]/curve[i-1]-1 for i in range(1,len(curve))]
    vol=pstdev(rets) if len(rets)>1 else 0
    sharpe=mean(rets)/vol*sqrt(252) if vol else 0.0
    return Result(round((curve[-1]-1)*100,2),round(dd*100,2),trades,
                  round(sharpe,2),round((prices[end]/prices[start-1]-1)*100,2))

def risk_gate(results):
    if len(results)<8: return False,"Fewer than 8 independent out-of-sample symbols"
    if any(r.max_drawdown_pct>12 for r in results): return False,"Drawdown above 12%"
    if mean(r.return_pct for r in results)<=0: return False,"Negative average OOS return"
    if mean(r.return_pct-r.buy_hold_pct for r in results)<=0:
        return False,"No average OOS outperformance vs buy-and-hold"
    if mean(r.sharpe for r in results)<=0.5: return False,"Average OOS Sharpe <= 0.5"
    return True,"Research gate passed; separate activation approval required"
