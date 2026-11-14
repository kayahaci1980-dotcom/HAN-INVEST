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
           VALUES ('2026-01-01T00:00:00Z', 'Binance', ?, 'SELL', 1, 1, 1, 0, ?, 'TEST')""",
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
               VALUES ('Binance', 'BTCUSDT', 1, 100.1, 100, 98, 110, 100,
                       '2026-01-01T00:00:00Z', 'LONG')"""
        )

    snapshot = paper_db._account_snapshot()
    assert snapshot["profit_usdt"] == pytest.approx(1.0)
    assert snapshot["loss_usdt"] == pytest.approx(0.2)
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
               VALUES ('Binance', 'ETHUSDT', 1, 100.1, 100, 102, 90, 90,
                       '2026-01-01T00:00:00Z', 'SHORT')"""
        )

    snapshot = paper_db._account_snapshot()
    assert snapshot["profit_usdt"] == pytest.approx(9.81)
    assert snapshot["loss_usdt"] == pytest.approx(0.0)
    assert snapshot["profit_usdt"] - snapshot["loss_usdt"] == pytest.approx(
        snapshot["equity_usdt"] - snapshot["initial_cash_usdt"]
    )
    assert snapshot["open_positions"][0]["side"] == "SHORT"
