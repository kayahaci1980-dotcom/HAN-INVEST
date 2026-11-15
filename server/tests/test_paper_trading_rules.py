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
            "ma20": 99.0,
            "ma50": 98.0,
            "daily_open": 100.0,
            "quote_volume_24h": 60_000_000,
            "atr_timeframe": "1D",
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
            # Open with normal ATR sizing, then top up the isolated test account
            # so it can exercise multiple equal-sized add-on tranches.
            self.assertTrue(
                paper._open_position(db, "Bybit", self.asset(), "TEST_SEED")
            )
            db.execute("UPDATE paper_account SET cash=5000 WHERE id=1")

    def test_low_score_does_not_block_long_entry(self):
        paper._apply_market_data("Bybit", {"assets": [self.asset(1)]}, True)
        self.assertEqual(len(self.positions()), 1)

    def test_score_change_does_not_close_existing_position(self):
        self.seed_position()
        paper._apply_market_data("Bybit", {"assets": [self.asset(1, 100.0)]}, False)
        self.assertEqual(len(self.positions()), 1)

    def test_loss_of_two_percent_starts_three_hour_cooldown_and_blocks_rebuy(self):
        self.seed_position()
        paper._apply_market_data("Bybit", {"assets": [self.asset(90, 98.0)]}, False)
        self.assertEqual(len(self.positions()), 0)
        cooldown = self.cooldown()
        self.assertIsNotNone(cooldown)
        remaining = float(cooldown["blocked_until"]) - paper._now().timestamp()
        self.assertGreater(remaining, 10_700)
        self.assertLessEqual(remaining, 10_800)
        paper._apply_market_data("Bybit", {"assets": [self.asset(90, 100.0)]}, True)
        self.assertEqual(len(self.positions()), 0)


    def test_adds_equal_initial_tranches_at_each_one_percent_milestone_once(self):
        self.seed_position()
        for price in (101.0, 102.0, 103.0):
            paper._apply_market_data("Bybit", {"assets": [self.asset(83, price)]}, False)
        with paper._connect() as db:
            lots = db.execute(
                "SELECT * FROM paper_position_lots WHERE exchange='Bybit' AND symbol='BTCUSDT' ORDER BY lot_index"
            ).fetchall()
            position = db.execute(
                "SELECT * FROM paper_positions WHERE exchange='Bybit' AND symbol='BTCUSDT'"
            ).fetchone()
            trades_before = db.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE reason LIKE 'SCALE_IN_%'"
            ).fetchone()["n"]
        self.assertEqual(len(lots), 4)
        self.assertAlmostEqual(float(lots[1]["cost_usdt"]), float(lots[0]["cost_usdt"]), places=6)
        self.assertAlmostEqual(float(lots[2]["cost_usdt"]), float(lots[0]["cost_usdt"]), places=6)
        self.assertAlmostEqual(float(lots[3]["cost_usdt"]), float(lots[0]["cost_usdt"]), places=6)
        paper._apply_market_data("Bybit", {"assets": [self.asset(83, 103.0)]}, False)
        with paper._connect() as db:
            trades_after = db.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE reason LIKE 'SCALE_IN_%'"
            ).fetchone()["n"]
        self.assertEqual(trades_before, trades_after)
        self.assertEqual(int(position["next_add_level"]), 4)

    def test_hard_stop_at_half_percent_closes_position_and_starts_three_hour_cooldown(self):
        self.seed_position()
        stop_price = 100.0 * 1.001 * 0.995 / 0.999
        paper._apply_market_data("Bybit", {"assets": [self.asset(83, stop_price)]}, False)
        self.assertEqual(len(self.positions()), 0)
        cooldown = self.cooldown()
        self.assertIsNotNone(cooldown)
        self.assertAlmostEqual(float(cooldown["loss_pct"]), -0.5, places=5)
        remaining = float(cooldown["blocked_until"]) - paper._now().timestamp()
        self.assertGreater(remaining, 10_700)
        self.assertLessEqual(remaining, 10_800)

    def test_hard_stop_has_priority_over_adverse_partial_reduction(self):
        self.seed_position()
        stop_price = 100.0 * 1.001 * 0.995 / 0.999
        paper._apply_market_data("Bybit", {"assets": [self.asset(1, stop_price)]}, False)
        self.assertEqual(len(self.positions()), 0)

    def test_daily_atr_chase_filter_blocks_long_after_half_atr_move(self):
        asset = self.asset(1, 101.0)
        asset["daily_open"] = 100.0
        asset["atr_14"] = 2.0
        asset["atr_14_1d"] = 2.0
        paper._apply_market_data("Bybit", {"assets": [asset]}, True)
        self.assertEqual(len(self.positions()), 0)

    def test_short_entry_is_score_independent_and_uses_one_hundred_usdt_base(self):
        asset = self.asset(0, 99.9)
        asset["ma20"] = 101.0
        asset["ma50"] = 102.0
        paper._apply_market_data("Bybit", {"assets": [asset]}, False, True)
        positions = self.positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["direction"], "SHORT")
        self.assertAlmostEqual(float(positions[0]["cost_usdt"]), 100.0, places=5)

    def test_short_hard_stop_starts_three_hour_cooldown(self):
        asset = self.asset(0, 99.9)
        asset["ma20"] = 101.0
        asset["ma50"] = 102.0
        with paper._connect() as db:
            self.assertTrue(paper._open_short_position(db, "Bybit", asset, "TEST_SHORT"))
        # Opening and estimated closing fees are included in the -0.5% stop.
        paper._apply_market_data("Bybit", {"assets": [self.asset(0, 100.6)]}, False, True)
        self.assertEqual(len(self.positions()), 0)
        self.assertIsNotNone(self.cooldown())

    def test_short_adds_one_equal_tranche_after_each_favorable_one_percent(self):
        asset = self.asset(0, 99.9)
        asset["ma20"] = 101.0
        asset["ma50"] = 102.0
        with paper._connect() as db:
            self.assertTrue(paper._open_short_position(db, "Bybit", asset, "TEST_SHORT"))
            db.execute("UPDATE paper_account SET cash=5000 WHERE id=1")
        for price in (98.9, 97.9):
            paper._apply_market_data("Bybit", {"assets": [self.asset(0, price)]}, False, True)
        with paper._connect() as db:
            lots = db.execute("SELECT * FROM paper_position_lots ORDER BY lot_index").fetchall()
        self.assertEqual(len(lots), 3)
        self.assertAlmostEqual(float(lots[1]["cost_usdt"]), float(lots[0]["cost_usdt"]), places=5)

    def test_short_accounting_uses_each_lot_entry_for_unrealized_and_realized_pnl(self):
        asset = self.asset(0, 100.0)
        asset["ma20"] = 101.0
        asset["ma50"] = 102.0
        with paper._connect() as db:
            self.assertTrue(paper._open_short_position(db, "Bybit", asset, "TEST_SHORT"))
            db.execute("UPDATE paper_account SET cash=5000 WHERE id=1")
            position = db.execute("SELECT * FROM paper_positions").fetchone()
            self.assertTrue(paper._add_scale_lot(db, position, 99.0))
            db.execute("UPDATE paper_positions SET last_price=98.0 WHERE exchange='Bybit' AND symbol='BTCUSDT'")
            lots = db.execute("SELECT * FROM paper_position_lots ORDER BY lot_index").fetchall()
        expected_gross = sum((float(x["entry_price"]) - 98.0) * float(x["quantity"]) for x in lots)
        expected_open_fee = sum(float(x["cost_usdt"]) - float(x["entry_price"]) * float(x["quantity"]) for x in lots)
        snapshot = paper._account_snapshot()
        self.assertAlmostEqual(snapshot["open_positions"][0]["unrealized_pnl_usdt"],
                               expected_gross - expected_open_fee -
                               sum(float(x["quantity"]) for x in lots) * 98.0 * paper._FEE_RATE,
                               places=6)
        with paper._connect() as db:
            position = db.execute("SELECT * FROM paper_positions").fetchone()
            paper._close_position(db, position, 98.0, "TEST_CLOSE")
            trade = db.execute("SELECT * FROM paper_trades WHERE side='SHORT_COVER' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertAlmostEqual(float(trade["pnl_usdt"]),
                               expected_gross - expected_open_fee -
                               sum(float(x["quantity"]) for x in lots) * 98.0 * paper._FEE_RATE,
                               places=6)

    def test_atr_take_profit_closes_long_at_two_to_one_target(self):
        self.seed_position()
        paper._apply_market_data("Bybit", {"assets": [self.asset(1, 103.0)]}, False)
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute("SELECT * FROM paper_trades WHERE pnl_usdt IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(trade["reason"], "ATR_TAKE_PROFIT_RR_1_2")

    def test_atr_take_profit_closes_short_at_two_to_one_target(self):
        asset = self.asset(0, 100.0)
        asset["ma20"] = 101.0
        asset["ma50"] = 102.0
        with paper._connect() as db:
            self.assertTrue(paper._open_short_position(db, "Bybit", asset, "TEST_SHORT"))
        paper._apply_market_data("Bybit", {"assets": [self.asset(0, 97.0)]}, False, True)
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute("SELECT * FROM paper_trades WHERE side='SHORT_COVER' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(trade["reason"], "ATR_TAKE_PROFIT_RR_1_2")

    def test_volume_must_be_strictly_above_fifty_million(self):
        asset = self.asset(1, 100.0)
        asset["quote_volume_24h"] = 50_000_000
        paper._apply_market_data("Bybit", {"assets": [asset]}, True)
        self.assertEqual(len(self.positions()), 0)



if __name__ == "__main__":
    unittest.main()
