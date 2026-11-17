"""Research-only aggressive PAPER strategy. Never imported by the live simulator."""
from dataclasses import dataclass
from math import sqrt
from statistics import mean, pstdev

FEE_BPS = 15  # simulated round-trip costs are charged per side
MAX_EXPOSURE = 0.65
STOP_PCT = 0.035
TRAIL_PCT = 0.045

@dataclass
class Result:
    return_pct: float
    max_drawdown_pct: float
    trades: int
    sharpe: float
    buy_hold_pct: float

def backtest(prices, fee_bps=FEE_BPS):
    """Daily walk-forward: yesterday's completed close sets next day's exposure.
    Signals only use prices available at that point. No intraday claims.
    """
    if len(prices) < 90 or any(p <= 0 for p in prices):
        raise ValueError("At least 90 strictly positive closes required")
    if not 0 <= fee_bps <= 200:
        raise ValueError("Unreasonable fee assumption")
    cash, shares, peak, entry, high, trades = 1.0, 0.0, 1.0, 0.0, 0.0, 0
    equity_curve = [1.0]
    daily_returns = []
    for i in range(60, len(prices) - 1):
        p, next_p = prices[i], prices[i+1]
        sma10 = mean(prices[i-9:i+1])
        sma30 = mean(prices[i-29:i+1])
        momentum = p / prices[i-10] - 1
        equity = cash + shares * p
        if shares:
            high = max(high, p)
            sell = p <= entry*(1-STOP_PCT) or p <= high*(1-TRAIL_PCT) or p < sma10
            if sell:
                cash += shares*p*(1-fee_bps/10000)
                shares = 0.0
                trades += 1
        elif p > sma10 > sma30 and momentum > 0.01:
            spend = min(cash, equity*MAX_EXPOSURE)
            if spend > 0:
                shares = spend*(1-fee_bps/10000)/p
                cash -= spend
                entry = high = p
                trades += 1
        # Execute signal at close i, mark to next close; no access to future signal.
        next_equity = cash + shares*next_p
        daily_returns.append(next_equity/equity_curve[-1]-1)
        equity_curve.append(next_equity)
        peak = max(peak,next_equity)
    peaks = [equity_curve[0]]
    for x in equity_curve[1:]: peaks.append(max(peaks[-1],x))
    drawdown = max((1-v/p for v,p in zip(equity_curve,peaks)), default=0)
    volatility = pstdev(daily_returns) if len(daily_returns)>1 else 0
    sharpe = mean(daily_returns)/volatility*sqrt(252) if volatility else 0.0
    return Result(round((equity_curve[-1]-1)*100,2),round(drawdown*100,2),
                  trades,round(sharpe,2),round((prices[-1]/prices[60]-1)*100,2))

def risk_gate(results):
    """Conservative deployment gate for an otherwise aggressive research profile."""
    if len(results) < 8:
        return False, "Fewer than 8 symbols with valid backtests"
    if any(r.max_drawdown_pct > 12 for r in results):
        return False, "One or more symbols exceeded 12% drawdown"
    if mean(r.return_pct-r.buy_hold_pct for r in results) <= 0:
        return False, "No average net outperformance vs buy-and-hold"
    if mean(r.sharpe for r in results) <= 0.5:
        return False, "Average Sharpe <= 0.5"
    return True, "Research gate passed; still requires manual activation"
