import tempfile
import unittest
from pathlib import Path

from app import paper_trading as paper


class PaperTradingRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = paper._DB_PATH
        self.old_database_url = paper._DATABASE_URL
        self.old_storage_persistent = paper._STORAGE_PERSISTENT
        paper._DATABASE_URL = ""
        paper._DB_PATH = Path(self.temp_dir.name) / "paper-test.sqlite3"
        paper._STORAGE_PERSISTENT = False
        paper._init_db()
        with paper._connect() as db:
            db.execute("UPDATE paper_account SET enabled=1, cash=1000 WHERE id=1")

    def tearDown(self):
        paper._DB_PATH = self.old_db_path
        paper._DATABASE_URL = self.old_database_url
        paper._STORAGE_PERSISTENT = self.old_storage_persistent
        self.temp_dir.cleanup()

    def asset(self, score=83, price=100.0):
        return {
            "symbol": "BTCUSDT",
            "price": price,
            "atr_14": 1.0,
            "score": score,
            "trade_ready": True,
        }

    def positions(self):
        with paper._connect() as db:
            return db.execute("SELECT * FROM paper_positions").fetchall()

    def cooldown(self):
        with paper._connect() as db:
            return db.execute(
                "SELECT * FROM paper_loss_cooldowns WHERE exchange='Bybit' AND symbol='BTCUSDT'"
            ).fetchone()

    def seed_position(self):
        with paper._connect() as db:
            self.assertTrue(
                paper._open_position(db, "Bybit", self.asset(), "TEST_SEED")
            )

    def test_score_83_opens_position(self):
        paper._apply_market_data("Bybit", {"assets": [self.asset(83)]}, True)
        self.assertEqual(len(self.positions()), 1)

    def test_score_82_does_not_open_position(self):
        paper._apply_market_data("Bybit", {"assets": [self.asset(82)]}, True)
        self.assertEqual(len(self.positions()), 0)

    def test_existing_position_holds_at_83_and_closes_at_82(self):
        self.seed_position()
        paper._apply_market_data("Bybit", {"assets": [self.asset(83)]}, False)
        self.assertEqual(len(self.positions()), 1)
        paper._apply_market_data("Bybit", {"assets": [self.asset(82)]}, False)
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute(
                "SELECT reason FROM paper_trades WHERE side='SELL' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(trade["reason"], "SCORE_AT_OR_BELOW_82")

    def short_asset(self, score=65, price=100.0):
        return {
            'symbol': 'ETHUSDT', 'price': price, 'short_score': score,
            'short_ready': True, 'trade_ready': True,
        }

    def test_short_score_65_opens_virtual_short(self):
        paper._apply_short_market_data({'assets': [self.short_asset(65)]}, True)
        with paper._connect() as db:
            rows = db.execute('SELECT * FROM paper_short_positions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['symbol'], 'ETHUSDT')

    def test_short_score_64_does_not_open(self):
        paper._apply_short_market_data({'assets': [self.short_asset(64)]}, True)
        with paper._connect() as db:
            rows = db.execute('SELECT * FROM paper_short_positions').fetchall()
        self.assertEqual(len(rows), 0)

    def test_short_stop_closes_when_price_rises_two_percent(self):
        paper._apply_short_market_data({'assets': [self.short_asset(90, 100.0)]}, True)
        paper._apply_short_market_data({'assets': [self.short_asset(90, 103.0)]}, False)
        with paper._connect() as db:
            rows = db.execute('SELECT * FROM paper_short_positions').fetchall()
            trade = db.execute("SELECT reason FROM paper_trades WHERE side='SHORT_CLOSE' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(len(rows), 0)
        self.assertEqual(trade['reason'], 'SHORT_STOP_LOSS_2_PERCENT')

    def test_loss_of_two_percent_starts_eight_hour_cooldown_and_blocks_rebuy(self):
        self.seed_position()
        paper._apply_market_data("Bybit", {"assets": [self.asset(90, 98.0)]}, False)
        self.assertEqual(len(self.positions()), 0)
        cooldown = self.cooldown()
        self.assertIsNotNone(cooldown)
        remaining = float(cooldown["blocked_until"]) - paper._now().timestamp()
        self.assertGreater(remaining, 28_700)
        self.assertLessEqual(remaining, 28_800)
        paper._apply_market_data("Bybit", {"assets": [self.asset(90, 100.0)]}, True)
        self.assertEqual(len(self.positions()), 0)


    def test_account_snapshot_unifies_realized_and_unrealized_profit_loss(self):
        self.seed_position()
        with paper._connect() as db:
            db.execute("UPDATE paper_positions SET last_price=102.0 WHERE exchange='Bybit' AND symbol='BTCUSDT'")
            # A closed winner (+3) and loser (-2) have already changed cash by net +1.
            db.execute("UPDATE paper_account SET cash=cash+1.0 WHERE id=1")
            for symbol, side, pnl in (("WINUSDT", "SELL", 3.0), ("LOSSUSDT", "SHORT_CLOSE", -2.0)):
                db.execute(
                    """INSERT INTO paper_trades
                       (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
                       VALUES (?, 'Bybit', ?, ?, 100.0, 1.0, 100.0, 0.1, ?, 'TEST')""",
                    (paper._now(), symbol, side, pnl),
                )

        state = paper._account_snapshot()
        self.assertAlmostEqual(state["realized_profit_usdt"], 3.0, places=6)
        self.assertAlmostEqual(state["realized_loss_usdt"], 2.0, places=6)
        self.assertGreater(state["unrealized_profit_usdt"], 0.0)
        self.assertAlmostEqual(
            state["profit_usdt"] - state["loss_usdt"],
            state["equity_usdt"] - state["initial_cash_usdt"],
            places=6,
        )
        self.assertAlmostEqual(
            state["net_pnl_usdt"],
            state["profit_usdt"] - state["loss_usdt"],
            places=6,
        )

if __name__ == "__main__":
    unittest.main()
