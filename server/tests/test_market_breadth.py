from app.market_breadth import summarize_market_breadth


def coin(coin_id, symbol, change):
    return {
        "id": coin_id,
        "symbol": symbol,
        "quotes": {"USD": {"percent_change_24h": change}},
    }


def test_counts_unique_asset_once_and_excludes_stablecoins():
    rows = [
        coin("btc-bitcoin", "BTC", 2.0),
        coin("btc-bitcoin", "BTC", 2.0),  # same asset, repeated listing
        coin("eth-ethereum", "ETH", 1.0),
        coin("sol-solana", "SOL", -1.0),
        coin("usdt-tether", "USDT", 0.1),
    ]
    result = summarize_market_breadth(rows)
    assert result["total_coins"] == 3
    assert result["rising_coins"] == 2
    assert result["falling_coins"] == 1
    assert result["allow_new_entries"] is True


def test_falling_majority_blocks_new_entries():
    rows = [
        coin("btc-bitcoin", "BTC", -2.0),
        coin("eth-ethereum", "ETH", -1.0),
        coin("sol-solana", "SOL", 1.0),
    ]
    result = summarize_market_breadth(rows)
    assert result["status"] == "BLOCK_NEW_BUYS"
    assert result["allow_new_entries"] is False


def test_exact_half_or_neutral_market_waits():
    rows = [
        coin("btc-bitcoin", "BTC", 1.0),
        coin("eth-ethereum", "ETH", -1.0),
        coin("sol-solana", "SOL", 0.0),
        coin("ada-cardano", "ADA", 0.0),
    ]
    result = summarize_market_breadth(rows)
    assert result["status"] == "WAIT"
    assert result["allow_new_entries"] is False


def test_missing_market_data_is_not_counted():
    rows = [
        coin("btc-bitcoin", "BTC", None),
        coin("eth-ethereum", "ETH", float("nan")),
        coin("sol-solana", "SOL", 2.0),
    ]
    result = summarize_market_breadth(rows)
    assert result["total_coins"] == 1
    assert result["allow_new_entries"] is True
