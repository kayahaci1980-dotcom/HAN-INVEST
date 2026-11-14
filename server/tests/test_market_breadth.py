from app.market_breadth import summarize_market_breadth


def coin(coin_id, symbol, change):
    return {"id": coin_id, "symbol": symbol, "quotes": {"USD": {"percent_change_24h": change}}}


def test_deduplicates_by_id_and_excludes_stablecoins():
    rows = [coin("btc", "BTC", 2), coin("btc", "BTC", 2), coin("eth", "ETH", 1),
            coin("sol", "SOL", -1), coin("usdt", "USDT", 0.1)]
    result = summarize_market_breadth(rows)
    assert (result["total_coins"], result["rising_coins"], result["falling_coins"]) == (3, 2, 1)
    assert result["status"] == "ALLOW_NEW_BUYS"
    assert result["allow_new_entries"] is True


def test_falling_majority_blocks_new_entries():
    rows = [coin("btc", "BTC", -2), coin("eth", "ETH", -1), coin("sol", "SOL", 1)]
    result = summarize_market_breadth(rows)
    assert result["status"] == "BLOCK_NEW_BUYS"
    assert result["allow_new_entries"] is False


def test_exact_half_or_neutral_market_waits():
    rows = [coin("btc", "BTC", 1), coin("eth", "ETH", -1),
            coin("sol", "SOL", 0), coin("ada", "ADA", 0)]
    result = summarize_market_breadth(rows)
    assert result["status"] == "WAIT"
    assert result["allow_new_entries"] is False


def test_missing_and_non_finite_data_is_ignored():
    rows = [coin("btc", "BTC", None), coin("eth", "ETH", float("nan")),
            coin("sol", "SOL", 2)]
    result = summarize_market_breadth(rows)
    assert result["total_coins"] == 1
    assert result["allow_new_entries"] is True


def test_duplicate_id_is_counted_once_even_if_duplicate_value_differs():
    rows = [coin("same", "AAA", 1), coin("same", "AAA", -1), coin("other", "BBB", 0)]
    result = summarize_market_breadth(rows)
    assert result["total_coins"] == 2
    assert result["rising_coins"] == 1
    assert result["falling_coins"] == 0
    assert result["unchanged_coins"] == 1
    assert result["status"] == "WAIT"
