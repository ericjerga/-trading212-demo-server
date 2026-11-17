import unittest
from research.offensive_strategy import backtest, risk_gate, Result

class OffensiveStrategyTests(unittest.TestCase):
    def test_requires_history(self):
        with self.assertRaises(ValueError): backtest([100.0]*20)
    def test_flat_market_no_trades(self):
        r=backtest([100.0]*120)
        self.assertEqual(r.trades,0)
        self.assertEqual(r.return_pct,0)
    def test_costs_reduce_returns(self):
        prices=[100+i*.5 for i in range(130)]
        self.assertLessEqual(backtest(prices,30).return_pct,backtest(prices,0).return_pct)
    def test_risk_gate_rejects_drawdown(self):
        rows=[Result(10,13,5,1,2)]*8
        self.assertFalse(risk_gate(rows)[0])
    def test_risk_gate_rejects_small_sample(self):
        self.assertFalse(risk_gate([Result(10,1,5,1,2)])[0])
    def test_strategy_is_exposure_capped(self):
        self.assertLess(backtest([100+i for i in range(130)]).return_pct, 100)

if __name__=="__main__": unittest.main()
