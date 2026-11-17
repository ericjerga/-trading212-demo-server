"""Research-only chronological train/test validation. No trading endpoints."""
import json
from statistics import mean
from app import WATCHLIST, fetch_closes
from research.offensive_strategy import backtest, risk_gate

CANDIDATES=((8,24,0.035,0.045),(10,30,0.035,0.045),
            (15,40,0.04,0.06),(20,50,0.05,0.07))
def evaluate(data, params, segment):
    fast,slow,stop,trail=params
    out=[]
    for symbol,prices,source in data:
        split=max(100,int(len(prices)*.70))
        start,end=(60,split-1) if segment=="train" else (split,len(prices)-1)
        try:
            result=backtest(prices,fast=fast,slow=slow,stop_pct=stop,trail_pct=trail,
                            start=start,end=end)
            out.append({"symbol":symbol,"source":source,**vars(result)})
        except ValueError: pass
    return out

def main():
    data,errors=[],[]
    for symbol,_ in WATCHLIST:
        try:
            history,source=fetch_closes(symbol)
            data.append((symbol,[p for _,p in history],source))
        except Exception as exc:
            errors.append({"symbol":symbol,"error":type(exc).__name__})
    # Pick ONE parameter set using training segments only; OOS never tunes parameters.
    rankings=[]
    for params in CANDIDATES:
        train=evaluate(data,params,"train")
        if len(train)<8: continue
        score=mean(x["return_pct"]-x["buy_hold_pct"] for x in train)
        rankings.append((score,params))
    chosen=max(rankings,key=lambda x:x[0])[1] if rankings else None
    train=evaluate(data,chosen,"train") if chosen else []
    test=evaluate(data,chosen,"test") if chosen else []
    from research.offensive_strategy import Result
    results=[Result(*(r[k] for k in ("return_pct","max_drawdown_pct","trades","sharpe","buy_hold_pct"))) for r in test]
    ok,reason=risk_gate(results)
    report={"profile":"offensive-research-only","validation":"chronological 70% train / 30% out-of-sample per symbol","selected_parameters":chosen,"candidate_count":len(CANDIDATES),"execution":"previous close signal, next close proxy fill","costs":"15 bps fees + 15 bps slippage per side","limits":"daily closes only; not an intraday stop, no live order book, same-market cross-sectional dependence","training":train,"out_of_sample":test,"errors":errors,"out_of_sample_average_return_pct":round(mean(r["return_pct"] for r in test),2) if test else None,"risk_gate_passed":ok,"risk_gate_reason":reason}
    with open("offensive-backtest-report.json","w") as f: json.dump(report,f,indent=2)
    print(json.dumps(report,indent=2))
    if not ok: raise SystemExit("Out-of-sample risk gate failed; DO NOT activate")

if __name__=="__main__": main()
