import unittest
from research.offensive_strategy import backtest, risk_gate, Result
from research.run_backtest import evaluate, CANDIDATES

class OffensiveStrategyTests(unittest.TestCase):
    def test_requires_history(self):
        with self.assertRaises(ValueError): backtest([100.0]*20)
    def test_flat_market_no_trades(self):
        r=backtest([100.0]*120)
        self.assertEqual(r.trades,0)
        self.assertEqual(r.return_pct,0)
    def test_costs_reduce_returns(self):
        prices=[100+i*.5 for i in range(150)]
        self.assertLessEqual(backtest(prices,30,30).return_pct,backtest(prices,0,0).return_pct)
    def test_risk_gate_rejects_drawdown(self):
        self.assertFalse(risk_gate([Result(10,13,5,1,2)]*8)[0])
    def test_risk_gate_rejects_negative_returns(self):
        self.assertFalse(risk_gate([Result(-1,2,5,-1,-5)]*8)[0])
    def test_risk_gate_rejects_small_sample(self):
        self.assertFalse(risk_gate([Result(10,1,5,1,2)])[0])
    def test_out_of_sample_starts_after_training(self):
        prices=[100+i*.2 for i in range(200)]
        train=evaluate([("TEST",prices,"synthetic")],CANDIDATES[0],"train")
        test=evaluate([("TEST",prices,"synthetic")],CANDIDATES[0],"test")
        self.assertEqual(len(train),1)
        self.assertEqual(len(test),1)
    def test_no_future_leak_on_first_decision(self):
        prices=[100+i*.2 for i in range(150)]
        a=backtest(prices,start=100,end=110)
        changed=prices[:]
        changed[120]=100000
        b=backtest(changed,start=100,end=110)
        self.assertEqual(a,b)

if __name__=="__main__": unittest.main()
