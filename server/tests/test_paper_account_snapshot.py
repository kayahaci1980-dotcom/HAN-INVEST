import pytest

from app import paper_trading as paper


@pytest.fixture
def paper_db(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "_DATABASE_URL", "")
    monkeypatch.setattr(paper, "_DB_PATH", tmp_path / "paper.sqlite3")
    monkeypatch.setattr(paper, "_STORAGE_PERSISTENT", False)
    paper._init_db()
    return paper


def _add_closed_trade(db, pnl, symbol):
    db.execute(
        """INSERT INTO paper_trades
           (time, exchange, symbol, side, price, quantity, value_usdt, fee_usdt, pnl_usdt, reason)
           VALUES ('2026-01-01T00:00:00Z', 'Bybit', ?, 'SELL', 1, 1, 1, 0, ?, 'TEST')""",
        (symbol, pnl),
    )


def test_snapshot_separates_profit_and_loss_and_matches_equity(paper_db):
    with paper_db._connect() as db:
        db.execute("UPDATE paper_account SET cash=900.9 WHERE id=1")
        _add_closed_trade(db, 3.0, "WIN")
        _add_closed_trade(db, -2.0, "LOSS")
        db.execute(
            """INSERT INTO paper_positions
               (exchange, symbol, quantity, cost_usdt, entry_price, stop_loss,
                take_profit, last_price, opened_at, direction)
               VALUES ('Bybit', 'BTCUSDT', 1, 100.1, 100, 98, 110, 100,
                       '2026-01-01T00:00:00Z', 'LONG')"""
        )

    snapshot = paper_db._account_snapshot()
    assert snapshot["profit_usdt"] == pytest.approx(3.0)
    assert snapshot["loss_usdt"] == pytest.approx(2.2)
    assert snapshot["equity_usdt"] - snapshot["initial_cash_usdt"] == pytest.approx(0.8)
    assert snapshot["profit_usdt"] - snapshot["loss_usdt"] == pytest.approx(
        snapshot["equity_usdt"] - snapshot["initial_cash_usdt"]
    )
    assert snapshot["open_positions"][0]["unrealized_pnl_usdt"] == pytest.approx(-0.2)


def test_short_snapshot_includes_both_fees(paper_db):
    with paper_db._connect() as db:
        db.execute("UPDATE paper_account SET cash=899.9 WHERE id=1")
        db.execute(
            """INSERT INTO paper_positions
               (exchange, symbol, quantity, cost_usdt, entry_price, stop_loss,
                take_profit, last_price, opened_at, direction)
               VALUES ('Bybit', 'ETHUSDT', 1, 100.1, 100, 102, 90, 90,
                       '2026-01-01T00:00:00Z', 'SHORT')"""
        )

    snapshot = paper_db._account_snapshot()
    assert snapshot["profit_usdt"] == pytest.approx(9.81)
    assert snapshot["loss_usdt"] == pytest.approx(0.0)
    assert snapshot["profit_usdt"] - snapshot["loss_usdt"] == pytest.approx(
        snapshot["equity_usdt"] - snapshot["initial_cash_usdt"]
    )
    assert snapshot["open_positions"][0]["side"] == "SHORT"

def test_scaled_short_displays_weighted_average_entry_consistent_with_pnl(paper_db):
    with paper_db._connect() as db:
        db.execute("UPDATE paper_account SET cash=719.72 WHERE id=1")
        db.execute(
            """INSERT INTO paper_positions
               (exchange, symbol, quantity, cost_usdt, entry_price, stop_loss,
                take_profit, last_price, opened_at, direction)
               VALUES ('Bybit', 'TESTUSDT', 3, 280.28, 100, 102, 0, 95,
                       '2026-01-01T00:00:00Z', 'SHORT')"""
        )
        db.execute(
            """INSERT INTO paper_position_lots
               (exchange, symbol, lot_index, quantity, cost_usdt, entry_price, opened_at)
               VALUES ('Bybit', 'TESTUSDT', 0, 1, 100.1, 100, '2026-01-01T00:00:00Z')"""
        )
        db.execute(
            """INSERT INTO paper_position_lots
               (exchange, symbol, lot_index, quantity, cost_usdt, entry_price, opened_at)
               VALUES ('Bybit', 'TESTUSDT', 1, 2, 180.18, 90, '2026-01-01T01:00:00Z')"""
        )

    snapshot = paper_db._account_snapshot()
    position = snapshot["open_positions"][0]
    assert position["base_entry_price"] == pytest.approx(100.0)
    assert position["average_entry_price"] == pytest.approx(280.0 / 3.0)
    assert position["entry_price"] == pytest.approx(280.0 / 3.0)
    assert position["unrealized_pnl_usdt"] == pytest.approx(-5.565)
    assert snapshot["equity_usdt"] - snapshot["initial_cash_usdt"] == pytest.approx(-5.565)
