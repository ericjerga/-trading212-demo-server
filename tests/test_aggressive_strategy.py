"""Focused regression tests for experimental paper-only trading signals."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import app as bot


class AggressivePaperStrategyTests(unittest.TestCase):
    def test_cooldown_allows_first_trade(self):
        self.assertTrue(bot.cooldown_elapsed(None, datetime.now(timezone.utc), 2))

    def test_cooldown_blocks_rapid_repeat(self):
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        self.assertFalse(bot.cooldown_elapsed((now - timedelta(minutes=30)).isoformat(), now, 2))
        self.assertTrue(bot.cooldown_elapsed((now - timedelta(hours=3)).isoformat(), now, 2))

    def test_invalid_timestamp_fails_closed(self):
        self.assertFalse(bot.cooldown_elapsed("invalid", datetime.now(timezone.utc), 2))
        self.assertFalse(bot.cooldown_elapsed("2026-11-11T12:00:00", datetime.now(timezone.utc), 2))

    def test_early_momentum_and_news_gate(self):
        row = dict(available=True, daily_change=1.2, volatility_pct=2.5,
                   price=99.8, sma20=100, sma50=100.5)
        self.assertTrue(bot.momentum_candidate(row, 0))
        self.assertFalse(bot.momentum_candidate(row, None))
        self.assertFalse(bot.momentum_candidate(row, -1))
        self.assertFalse(bot.momentum_candidate({**row, "available": False}, 0))
        self.assertFalse(bot.momentum_candidate({**row, "volatility_pct": 8}, 0))

    def test_no_real_trading_routes(self):
        paths = {rule.rule for rule in bot.app.url_map.iter_rules()}
        self.assertNotIn("/order", paths)
        self.assertNotIn("/broker/order", paths)

    def test_health_stale_when_no_steps(self):
        with bot.app.test_client() as client:
            with bot.LOCK:
                old = bot.state["last_step_at"]
                bot.state["last_step_at"] = None
            try:
                response = client.get("/health")
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.get_json()["simulation_recent"])
                self.assertFalse(response.get_json()["real_orders_enabled"])
            finally:
                with bot.LOCK:
                    bot.state["last_step_at"] = old


if __name__ == "__main__":
    unittest.main()
