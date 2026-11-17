"""Historical daily-close research. No paper orders, no ledger mutations."""
import json
from statistics import mean
from app import WATCHLIST, fetch_closes
from research.offensive_strategy import backtest, risk_gate

def main():
    rows, errors = [], []
    for symbol, _ in WATCHLIST:
        try:
            history, source = fetch_closes(symbol)
            result = backtest([price for _,price in history])
            rows.append({"symbol":symbol,"source":source,**vars(result)})
        except Exception as exc:
            errors.append({"symbol":symbol,"error":type(exc).__name__})
    ok,reason = risk_gate([backtest_result(r) for r in rows])
    report={"profile":"max-offensive-research-only","assumptions":{"bar":"daily close","exposure_cap":0.65,"fee_bps_per_side":15,"stop_pct":3.5,"trailing_stop_pct":4.5,"slippage_model":"fixed 15bps per side (cost proxy)","intraday":"not tested"},"symbols":rows,"errors":errors,"average_return_pct":round(mean(r["return_pct"] for r in rows),2) if rows else None,"risk_gate_passed":ok,"risk_gate_reason":reason}
    print(json.dumps(report,indent=2))
    with open("offensive-backtest-report.json","w") as f: json.dump(report,f,indent=2)
    if not ok: raise SystemExit("Risk gate failed; DO NOT activate")

def backtest_result(row):
    from research.offensive_strategy import Result
    return Result(*(row[k] for k in ("return_pct","max_drawdown_pct","trades","sharpe","buy_hold_pct")))

if __name__=="__main__": main()
