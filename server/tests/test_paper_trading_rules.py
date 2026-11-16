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

    def asset(self, score=83, price=100.0, symbol="BTCUSDT", volume=20_000_000):
        return {
            "symbol": symbol,
            "price": price,
            "atr_14": 1.0,
            "atr_14_1h": 1.0,
            "atr_candle_time": "2026-11-15T10:00:00Z",
            "score": score,
            "trade_ready": True,
            "ma20": 99.0,
            "ma50": 98.0,
            "daily_open": 100.0,
            "quote_volume_24h": volume,
            "atr_timeframe": "1H",
            "candle_high_1h": price,
            "candle_low_1h": price,
        }

    def positions(self):
        with paper._connect() as db:
            return db.execute("SELECT * FROM paper_positions").fetchall()

    def cooldown(self, symbol="BTCUSDT"):
        with paper._connect() as db:
            return db.execute(
                "SELECT * FROM paper_loss_cooldowns WHERE exchange='Bybit' AND symbol=?",
                (symbol,),
            ).fetchone()

    def seed_position(self, direction="LONG"):
        asset = self.asset()
        with paper._connect() as db:
            opened = (
                paper._open_position(db, "Bybit", asset, "TEST_SEED")
                if direction == "LONG"
                else paper._open_short_position(db, "Bybit", asset, "TEST_SEED")
            )
            self.assertTrue(opened)
            db.execute("UPDATE paper_account SET cash=5000 WHERE id=1")

    def test_score_60_allows_long_entry(self):
        paper._apply_market_data("Bybit", {"assets": [self.asset(60)]}, True)
        self.assertEqual(len(self.positions()), 1)

    def test_score_below_60_does_not_block_long_entry(self):
        paper._apply_market_data("Bybit", {"assets": [self.asset(1)]}, False, False)
        self.assertEqual(len(self.positions()), 1)

    def test_score_change_does_not_close_existing_position(self):
        self.seed_position()
        paper._apply_market_data("Bybit", {"assets": [self.asset(1, 100.0)]}, False)
        self.assertEqual(len(self.positions()), 1)

    def test_initial_long_stop_limits_net_loss_to_fixed_one_usdt_including_fees(self):
        self.seed_position()
        with paper._connect() as db:
            position = db.execute("SELECT * FROM paper_positions").fetchone()
            stop = paper._loss_limit_stop_price(db, position)
            net_pnl_at_stop = float(position["quantity"]) * stop * (1.0 - paper._FEE_RATE) - float(position["cost_usdt"])
            self.assertAlmostEqual(net_pnl_at_stop, -1.0, places=6)
            self.assertGreater(stop, float(position["stop_loss"]))
        self.assertAlmostEqual(float(position["initial_risk"]), 1.5, places=6)
        self.assertEqual(float(position["take_profit"]), 0.0)

    def test_initial_short_stop_limits_net_loss_to_fixed_one_usdt_including_fees(self):
        self.seed_position("SHORT")
        with paper._connect() as db:
            position = db.execute("SELECT * FROM paper_positions").fetchone()
            stop = paper._loss_limit_stop_price(db, position)
            lots = db.execute("SELECT quantity, cost_usdt, entry_price FROM paper_position_lots WHERE exchange=? AND symbol=?", ("Bybit", "BTCUSDT")).fetchall()
            entry_notional = sum(float(lot["entry_price"]) * float(lot["quantity"]) for lot in lots)
            opening_fee = sum(max(0.0, float(lot["cost_usdt"]) - float(lot["entry_price"]) * float(lot["quantity"])) for lot in lots)
            net_pnl_at_stop = entry_notional - opening_fee - stop * float(position["quantity"]) * (1.0 + paper._FEE_RATE)
            self.assertAlmostEqual(net_pnl_at_stop, -1.0, places=6)
            self.assertLess(stop, float(position["stop_loss"]))
        self.assertAlmostEqual(float(position["initial_risk"]), 1.5, places=6)
        self.assertEqual(float(position["take_profit"]), 0.0)

    def test_long_position_closes_at_one_percent_net_loss_including_fees(self):
        self.seed_position()
        with paper._connect() as db:
            position = db.execute("SELECT * FROM paper_positions").fetchone()
            stop = paper._loss_limit_stop_price(db, position)
        paper._apply_market_data("Bybit", {"assets": []}, False, False,
                                 position_prices={"BTCUSDT": stop})
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute("SELECT * FROM paper_trades WHERE pnl_usdt IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(trade["reason"], "STOP_LOSS")
        self.assertAlmostEqual(float(trade["pnl_usdt"]) / 100.0, -0.01, places=5)

    def test_short_position_closes_at_one_percent_net_loss_including_fees(self):
        self.seed_position("SHORT")
        with paper._connect() as db:
            position = db.execute("SELECT * FROM paper_positions").fetchone()
            stop = paper._loss_limit_stop_price(db, position)
        paper._apply_market_data("Bybit", {"assets": []}, False, False,
                                 position_prices={"BTCUSDT": stop})
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute("SELECT * FROM paper_trades WHERE side='SHORT_COVER' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(trade["reason"], "STOP_LOSS")
        self.assertAlmostEqual(float(trade["pnl_usdt"]) / 100.0, -0.01, places=5)

    def test_long_trailing_moves_to_breakeven_at_one_point_five_r(self):
        self.seed_position()
        asset = self.asset(price=102.25)
        asset["atr_candle_time"] = "2026-11-15T11:00:00Z"
        paper._apply_market_data("Bybit", {"assets": [asset]}, False)
        position = self.positions()[0]
        self.assertEqual(int(position["trailing_stage"]), 1)
        self.assertAlmostEqual(float(position["stop_loss"]), 100.0, places=6)

    def test_long_trailing_uses_two_atr_at_two_r(self):
        self.seed_position()
        first = self.asset(price=102.25)
        first["atr_candle_time"] = "2026-11-15T11:00:00Z"
        paper._apply_market_data("Bybit", {"assets": [first]}, False)
        second = self.asset(price=103.0)
        second["atr_candle_time"] = "2026-11-15T12:00:00Z"
        paper._apply_market_data("Bybit", {"assets": [second]}, False)
        position = self.positions()[0]
        self.assertEqual(int(position["trailing_stage"]), 2)
        self.assertGreater(float(position["stop_loss"]), 101.0)

    def test_long_trailing_uses_one_point_five_atr_at_three_r(self):
        self.seed_position()
        for hour, price in ((11, 102.25), (12, 103.0), (13, 104.5)):
            asset = self.asset(price=price)
            asset["atr_candle_time"] = f"2026-11-15T{hour:02d}:00:00Z"
            paper._apply_market_data("Bybit", {"assets": [asset]}, False)
        position = self.positions()[0]
        self.assertEqual(int(position["trailing_stage"]), 3)
        self.assertAlmostEqual(float(position["stop_loss"]), 103.0, places=6)

    def test_short_trailing_moves_to_breakeven_at_one_point_five_r(self):
        self.seed_position("SHORT")
        asset = self.asset(price=97.75)
        asset["ma20"], asset["ma50"] = 101.0, 102.0
        asset["atr_candle_time"] = "2026-11-15T11:00:00Z"
        paper._apply_market_data("Bybit", {"assets": [asset]}, False, True)
        position = self.positions()[0]
        self.assertEqual(int(position["trailing_stage"]), 1)
        self.assertAlmostEqual(float(position["stop_loss"]), 100.0, places=6)

    def test_short_trailing_uses_two_atr_at_two_r(self):
        self.seed_position("SHORT")
        for hour, price in ((11, 97.75), (12, 97.0)):
            asset = self.asset(price=price)
            asset["ma20"], asset["ma50"] = 101.0, 102.0
            asset["atr_candle_time"] = f"2026-11-15T{hour:02d}:00:00Z"
            paper._apply_market_data("Bybit", {"assets": [asset]}, False, True)
        position = self.positions()[0]
        self.assertEqual(int(position["trailing_stage"]), 2)
        self.assertLess(float(position["stop_loss"]), 99.0)

    def test_short_trailing_uses_one_point_five_atr_at_three_r(self):
        self.seed_position("SHORT")
        for hour, price in ((11, 97.75), (12, 97.0), (13, 95.5)):
            asset = self.asset(price=price)
            asset["ma20"], asset["ma50"] = 101.0, 102.0
            asset["atr_candle_time"] = f"2026-11-15T{hour:02d}:00:00Z"
            paper._apply_market_data("Bybit", {"assets": [asset]}, False, True)
        position = self.positions()[0]
        self.assertEqual(int(position["trailing_stage"]), 3)
        self.assertAlmostEqual(float(position["stop_loss"]), 97.0, places=6)

    def test_long_stop_loss_closes_at_initial_atr_stop_and_starts_four_hour_cooldown(self):
        self.seed_position()
        paper._apply_market_data("Bybit", {"assets": [self.asset(price=98.5)]}, False)
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute("SELECT * FROM paper_trades WHERE pnl_usdt IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(trade["reason"], "STOP_LOSS")
        cooldown = self.cooldown()
        self.assertIsNotNone(cooldown)
        remaining = float(cooldown["blocked_until"]) - paper._now().timestamp()
        self.assertGreater(remaining, 14_300)
        self.assertLessEqual(remaining, 14_400)

    def test_short_stop_loss_closes_at_initial_atr_stop_and_starts_four_hour_cooldown(self):
        self.seed_position("SHORT")
        asset = self.asset(price=101.5)
        asset["ma20"], asset["ma50"] = 101.0, 102.0
        paper._apply_market_data("Bybit", {"assets": [asset]}, False, True)
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute("SELECT * FROM paper_trades WHERE side='SHORT_COVER' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(trade["reason"], "STOP_LOSS")
        self.assertIsNotNone(self.cooldown())

    def test_direct_live_quote_closes_long_even_when_symbol_is_outside_analysis_universe(self):
        self.seed_position("LONG")
        paper._apply_market_data(
            "Bybit", {"assets": []}, False, False,
            position_prices={"BTCUSDT": 98.5},
        )
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute(
                "SELECT * FROM paper_trades WHERE pnl_usdt IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(trade["reason"], "STOP_LOSS")
        self.assertIsNotNone(self.cooldown())

    def test_direct_live_quote_closes_short_even_when_symbol_is_outside_analysis_universe(self):
        self.seed_position("SHORT")
        paper._apply_market_data(
            "Bybit", {"assets": []}, False, False,
            position_prices={"BTCUSDT": 101.5},
        )
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute(
                "SELECT * FROM paper_trades WHERE side='SHORT_COVER' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(trade["reason"], "STOP_LOSS")
        self.assertIsNotNone(self.cooldown())

    def test_two_consecutive_losses_block_only_same_symbol_for_eight_hours(self):
        with paper._connect() as db:
            for _ in range(2):
                self.assertTrue(paper._open_position(db, "Bybit", self.asset(), "TEST_SEED"))
                position = db.execute("SELECT * FROM paper_positions WHERE symbol='BTCUSDT'").fetchone()
                paper._close_position(db, position, 99.0, "MANUAL_CLOSE")
        cooldown = self.cooldown()
        self.assertIsNotNone(cooldown)
        remaining = float(cooldown["blocked_until"]) - paper._now().timestamp()
        self.assertGreater(remaining, 28_700)
        self.assertLessEqual(remaining, 28_800)
        self.assertIsNone(self.cooldown("ETHUSDT"))

    def test_daily_atr_chase_condition_does_not_block_long_entry(self):
        asset = self.asset(1, 101.0)
        asset["daily_open"] = 100.0
        asset["atr_14_1h"] = 2.0
        paper._apply_market_data("Bybit", {"assets": [asset]}, False, False)
        self.assertEqual(len(self.positions()), 1)

    def test_score_60_allows_short_entry(self):
        asset = self.asset(1, 99.9)
        asset["momentum_6h_pct"] = -0.1
        asset["ma20"], asset["ma50"] = 101.0, 102.0
        paper._apply_market_data("Bybit", {"assets": [asset]}, False, True)
        positions = self.positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["direction"], "SHORT")
        self.assertAlmostEqual(float(positions[0]["cost_usdt"]), 100.0, places=5)

    def test_score_below_60_does_not_block_short_entry(self):
        asset = self.asset(1, 99.9)
        asset["momentum_6h_pct"] = -0.1
        asset["ma20"], asset["ma50"] = 0.0, 0.0
        paper._apply_market_data("Bybit", {"assets": [asset]}, False, False)
        positions = self.positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["direction"], "SHORT")

    def test_adds_equal_initial_tranches_at_each_one_percent_milestone_once(self):
        self.seed_position()
        for hour, price in ((11, 101.0), (12, 102.0), (13, 102.5)):
            asset = self.asset(price=price)
            asset["atr_candle_time"] = f"2026-11-15T{hour:02d}:00:00Z"
            paper._apply_market_data("Bybit", {"assets": [asset]}, False)
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
        self.assertEqual(len(lots), 3)
        self.assertAlmostEqual(float(lots[1]["cost_usdt"]), float(lots[0]["cost_usdt"]), places=6)
        self.assertAlmostEqual(float(lots[2]["cost_usdt"]), float(lots[0]["cost_usdt"]), places=6)
        repeat = self.asset(price=102.5)
        repeat["atr_candle_time"] = "2026-11-15T13:00:00Z"
        paper._apply_market_data("Bybit", {"assets": [repeat]}, False)
        with paper._connect() as db:
            trades_after = db.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE reason LIKE 'SCALE_IN_%'"
            ).fetchone()["n"]
        self.assertEqual(trades_before, trades_after)
        self.assertEqual(int(position["next_add_level"]), 3)

    def test_short_adds_one_equal_tranche_after_each_favorable_one_percent(self):
        asset = self.asset(0, 100.0)
        asset["ma20"], asset["ma50"] = 101.0, 102.0
        with paper._connect() as db:
            self.assertTrue(paper._open_short_position(db, "Bybit", asset, "TEST_SHORT"))
            db.execute("UPDATE paper_account SET cash=5000 WHERE id=1")
        for hour, price in ((11, 99.0), (12, 98.0)):
            update = self.asset(0, price)
            update["ma20"], update["ma50"] = 101.0, 102.0
            update["atr_candle_time"] = f"2026-11-15T{hour:02d}:00:00Z"
            paper._apply_market_data("Bybit", {"assets": [update]}, False, True)
        with paper._connect() as db:
            lots = db.execute("SELECT * FROM paper_position_lots ORDER BY lot_index").fetchall()
        self.assertEqual(len(lots), 3)
        self.assertAlmostEqual(float(lots[1]["cost_usdt"]), float(lots[0]["cost_usdt"]), places=5)

    def test_short_accounting_uses_each_lot_entry_for_unrealized_and_realized_pnl(self):
        asset = self.asset(0, 100.0)
        asset["ma20"], asset["ma50"] = 101.0, 102.0
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
            paper._close_position(db, position, 98.0, "MANUAL_CLOSE")
            trade = db.execute("SELECT * FROM paper_trades WHERE side='SHORT_COVER' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertAlmostEqual(float(trade["pnl_usdt"]),
                               expected_gross - expected_open_fee -
                               sum(float(x["quantity"]) for x in lots) * 98.0 * paper._FEE_RATE,
                               places=6)

    def test_stablecoin_pairs_are_excluded_from_new_entries(self):
        for symbol in ("USDCUSDT", "USD1USDT", "USDTUSDT", "RLUSDUSDT", "USDC-USD"):
            with self.subTest(symbol=symbol):
                self.assertTrue(paper._is_stablecoin_pair(symbol))
        for symbol in ("BTCUSDT", "ETHUSDT", "APTUSDT", "SOLUSDT"):
            with self.subTest(symbol=symbol):
                self.assertFalse(paper._is_stablecoin_pair(symbol))

    def test_stablecoin_candidate_is_not_filtered_out(self):
        asset = self.asset(1, 100.0, symbol="USDCUSDT")
        paper._apply_market_data("Bybit", {"assets": [asset]}, False, False)
        self.assertEqual(len(self.positions()), 1)

    def test_volume_minimum_three_million_has_no_upper_cap(self):
        low_edge = self.asset(60, 100.0, symbol="EDGE3USDT", volume=3_000_000)
        high_volume = self.asset(60, 100.0, symbol="HIGH40USDT", volume=40_000_001)
        paper._apply_market_data("Bybit", {"assets": [low_edge, high_volume]}, True)
        self.assertEqual(len(self.positions()), 2)

    def test_volume_below_three_million_is_not_excluded(self):
        below = self.asset(60, 100.0, symbol="LOWUSDT", volume=2_999_999)
        paper._apply_market_data("Bybit", {"assets": [below]}, False, False)
        self.assertEqual(len(self.positions()), 1)

    def test_direction_entries_ignore_breadth_and_technical_indicators(self):
        long_asset = self.asset(score=0, symbol="LOWVOLUSDT", volume=1)
        long_asset["ma20"], long_asset["ma50"], long_asset["daily_open"] = 0, 0, 0
        short_asset = self.asset(score=0, symbol="SHORTUSDT", volume=1)
        short_asset["momentum_6h_pct"] = -0.5
        short_asset["ma20"], short_asset["ma50"], short_asset["daily_open"] = 0, 0, 0
        paper._apply_market_data("Bybit", {"assets": [long_asset, short_asset]}, False, False)
        positions = {row["symbol"]: row["direction"] for row in self.positions()}
        self.assertEqual(positions, {"LOWVOLUSDT": "LONG", "SHORTUSDT": "SHORT"})


    def test_long_closes_after_55_seconds_of_continuous_negative_pnl_and_blocks_15_minutes(self):
        self.seed_position("LONG")
        started = paper._now().timestamp() - 56
        with paper._connect() as db:
            db.execute(
                "UPDATE paper_positions SET loss_started_at=? WHERE exchange='Bybit' AND symbol='BTCUSDT'",
                (started,),
            )
        paper._apply_market_data(
            "Bybit", {"assets": []}, False, False,
            position_prices={"BTCUSDT": 99.8},
        )
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute(
                "SELECT * FROM paper_trades WHERE pnl_usdt IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(trade["reason"], "CONTINUOUS_LOSS_55S")
        cooldown = self.cooldown()
        remaining = float(cooldown["blocked_until"]) - paper._now().timestamp()
        self.assertGreater(remaining, 890)
        self.assertLessEqual(remaining, 900)

    def test_short_closes_after_55_seconds_of_continuous_negative_pnl_and_blocks_15_minutes(self):
        self.seed_position("SHORT")
        started = paper._now().timestamp() - 56
        with paper._connect() as db:
            db.execute(
                "UPDATE paper_positions SET loss_started_at=? WHERE exchange='Bybit' AND symbol='BTCUSDT'",
                (started,),
            )
        paper._apply_market_data(
            "Bybit", {"assets": []}, False, False,
            position_prices={"BTCUSDT": 100.2},
        )
        self.assertEqual(len(self.positions()), 0)
        with paper._connect() as db:
            trade = db.execute(
                "SELECT * FROM paper_trades WHERE side='SHORT_COVER' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(trade["reason"], "CONTINUOUS_LOSS_55S")
        cooldown = self.cooldown()
        remaining = float(cooldown["blocked_until"]) - paper._now().timestamp()
        self.assertGreater(remaining, 890)
        self.assertLessEqual(remaining, 900)

if __name__ == "__main__":
    unittest.main()
