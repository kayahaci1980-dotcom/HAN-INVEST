from app.market_breadth import summarize_market_breadth


def ticker(symbol, change):
    return {"symbol": symbol, "price24hPcnt": change}


def test_deduplicates_by_symbol_and_excludes_stablecoin_pairs():
    rows = [
        ticker("BTCUSDT", "0.02"),
        ticker("BTCUSDT", "0.02"),
        ticker("ETHUSDT", "0.01"),
        ticker("SOLUSDT", "-0.01"),
        ticker("USDCUSDT", "0.001"),
    ]
    result = summarize_market_breadth(rows)
    assert (result["total_coins"], result["rising_coins"], result["falling_coins"]) == (3, 2, 1)
    assert result["status"] == "LONG_BIAS"
    assert result["allow_long_entries"] is True
    assert result["allow_short_entries"] is False
    assert result["source"].startswith("Bybit Live Market")


def test_falling_majority_enables_short_only():
    rows = [ticker("BTCUSDT", "-0.02"), ticker("ETHUSDT", "-0.01"), ticker("SOLUSDT", "0.01")]
    result = summarize_market_breadth(rows)
    assert result["status"] == "SHORT_BIAS"
    assert result["allow_long_entries"] is False
    assert result["allow_short_entries"] is True


def test_exact_half_or_neutral_market_waits():
    rows = [
        ticker("BTCUSDT", "0.01"),
        ticker("ETHUSDT", "-0.01"),
        ticker("SOLUSDT", "0"),
        ticker("ADAUSDT", "0"),
    ]
    result = summarize_market_breadth(rows)
    assert result["status"] == "WAIT"
    assert result["allow_long_entries"] is False
    assert result["allow_short_entries"] is False


def test_missing_and_non_finite_changes_are_not_measured():
    rows = [
        ticker("BTCUSDT", None),
        ticker("ETHUSDT", "nan"),
        ticker("SOLUSDT", "0.02"),
    ]
    result = summarize_market_breadth(rows)
    assert result["total_coins"] == 3
    assert result["measured_coins"] == 1
    assert result["missing_change_coins"] == 2
    assert result["rising_coins"] == 1
    assert result["falling_coins"] == 0
    assert result["allow_long_entries"] is True


def test_duplicate_symbol_is_counted_once():
    rows = [ticker("AAAUSDT", "0.01"), ticker("AAAUSDT", "-0.01"), ticker("BBBUSDT", "0")]
    result = summarize_market_breadth(rows)
    assert result["total_coins"] == 2
    assert result["rising_coins"] == 1
    assert result["falling_coins"] == 0
    assert result["unchanged_coins"] == 1
    # One of two unique symbols is rising: exactly 50%, so LONG is allowed.
    assert result["status"] == "LONG_BIAS"
    assert result["allow_long_entries"] is True
    assert result["allow_short_entries"] is False


def test_unavailable_when_no_valid_usdt_pairs():
    result = summarize_market_breadth([ticker("USDCUSDT", "0.01"), ticker("BTCUSD", "0.01")])
    assert result["available"] is False
    assert result["status"] == "UNAVAILABLE"
    assert result["allow_long_entries"] is False
    assert result["allow_short_entries"] is False
