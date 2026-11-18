import unittest

from app.bybit import filter_tickers_by_min_volume


class BybitVolumeFilterTests(unittest.TestCase):
    def test_filters_tickers_below_three_million_turnover(self):
        tickers = [
            {"symbol": "AAAUSDT", "turnover24h": "2999999.99"},
            {"symbol": "BBBUSDT", "turnover24h": "3000000"},
            {"symbol": "CCCUSDT", "turnover24h": "40000001"},
            {"symbol": "DDDUSDT", "turnover24h": "NaN"},
            {"symbol": "EEEUSDT", "turnover24h": "invalid"},
            None,
        ]
        result = filter_tickers_by_min_volume(tickers, 3_000_000)
        self.assertEqual([item["symbol"] for item in result], ["BBBUSDT", "CCCUSDT"])

    def test_zero_minimum_preserves_all_valid_nonnegative_tickers(self):
        tickers = [
            {"symbol": "AAAUSDT", "turnover24h": "0"},
            {"symbol": "BBBUSDT", "turnover24h": "10"},
            {"symbol": "CCCUSDT", "turnover24h": "-1"},
        ]
        result = filter_tickers_by_min_volume(tickers, 0)
        self.assertEqual([item["symbol"] for item in result], ["AAAUSDT", "BBBUSDT"])

    def test_rejects_invalid_minimum(self):
        for value in (float("nan"), float("inf"), -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    filter_tickers_by_min_volume([], value)


if __name__ == "__main__":
    unittest.main()
