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
            # Leave enough virtual cash for several add-on tranches.
            db.execute("UPDATE paper_account SET cash=5000 WHERE id=1")
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

    def test_partial_reduction_sells_only_latest_add_on_lot(self):
        self.seed_position()
        with paper._connect() as db:
            position = db.execute(
                "SELECT * FROM paper_positions WHERE exchange='Bybit' AND symbol='BTCUSDT'"
            ).fetchone()
            self.assertTrue(paper._add_scale_lot(db, position, 101.0))
            position = db.execute(
                "SELECT * FROM paper_positions WHERE exchange='Bybit' AND symbol='BTCUSDT'"
            ).fetchone()
            base_quantity = float(db.execute(
                "SELECT quantity FROM paper_position_lots WHERE exchange='Bybit' AND symbol='BTCUSDT' AND lot_index=0"
            ).fetchone()["quantity"])
            add_quantity = float(db.execute(
                "SELECT quantity FROM paper_position_lots WHERE exchange='Bybit' AND symbol='BTCUSDT' AND lot_index=1"
            ).fetchone()["quantity"])
            self.assertTrue(paper._reduce_latest_scale_lot(db, position, 101.5))
            remaining = db.execute(
                "SELECT quantity FROM paper_positions WHERE exchange='Bybit' AND symbol='BTCUSDT'"
            ).fetchone()
            remaining_lots = db.execute(
                "SELECT COUNT(*) AS n FROM paper_position_lots WHERE exchange='Bybit' AND symbol='BTCUSDT'"
            ).fetchone()["n"]
        self.assertAlmostEqual(float(remaining["quantity"]), base_quantity, places=8)
        self.assertEqual(int(remaining_lots), 1)
        self.assertGreater(add_quantity, 0)



if __name__ == "__main__":
    unittest.main()
