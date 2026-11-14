import tempfile
import unittest
from pathlib import Path

from app import paper_trading as paper


class PaperAccountSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = paper._DB_PATH
        self.old_storage = paper._STORAGE_PERSISTENT
        paper._DB_PATH = Path(self.temp_dir.name) / "paper-test.sqlite3"
        paper._STORAGE_PERSISTENT = False
        paper._init_db()
        with paper._connect() as db:
            db.execute("UPDATE paper_account SET initial_cash=1000, cash=899.9 WHERE id=1")
            db.execute(
                """INSERT INTO paper_positions
                   (exchange, symbol, quantity, cost_usdt, entry_price,
                    stop_loss, take_profit, last_price, opened_at)
                   VALUES ('Bybit', 'BTCUSDT', 1.0, 100.1, 100.0,
                           98.0, 110.0, 102.0, ?)""",
                (paper._now(),),
            )
            # Realized net profit is +1.00 (+3 winner and -2 loser).
            for symbol, pnl in (("WINUSDT", 3.0), ("LOSSUSDT", -2.0)):
                db.execute(
                    """INSERT INTO paper_trades
                       (time, exchange, symbol, side, price, quantity,
                        value_usdt, fee_usdt, pnl_usdt, reason)
                       VALUES (?, 'Bybit', ?, 'SELL', 100.0, 1.0,
                               100.0, 0.1, ?, 'TEST')""",
                    (paper._now(), symbol, pnl),
                )
            db.execute("UPDATE paper_account SET cash=cash+1.0 WHERE id=1")

    def tearDown(self):
        paper._DB_PATH = self.old_db_path
        paper._STORAGE_PERSISTENT = self.old_storage
        self.temp_dir.cleanup()

    def test_profit_loss_split_matches_equity_and_uses_full_ledger(self):
        state = paper._account_snapshot()
        self.assertAlmostEqual(state["cash_usdt"], 900.9, places=6)
        self.assertAlmostEqual(state["holdings_value_usdt"], 102.0, places=6)
        self.assertAlmostEqual(state["equity_usdt"], 1002.9, places=6)
        self.assertAlmostEqual(state["realized_profit_usdt"], 3.0, places=6)
        self.assertAlmostEqual(state["realized_loss_usdt"], 2.0, places=6)
        self.assertAlmostEqual(state["unrealized_profit_usdt"], 1.9, places=6)
        self.assertAlmostEqual(state["profit_usdt"], 4.9, places=6)
        self.assertAlmostEqual(state["loss_usdt"], 2.0, places=6)
        self.assertAlmostEqual(state["net_pnl_usdt"], 2.9, places=6)
        self.assertAlmostEqual(
            state["profit_usdt"] - state["loss_usdt"],
            state["equity_usdt"] - state["initial_cash_usdt"],
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
