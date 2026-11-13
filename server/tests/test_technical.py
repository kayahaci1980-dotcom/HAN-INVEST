import unittest

from app.technical import calculate_indicators, score_indicators
from app.other_exchanges import indicators as exchange_indicators
from app.main import app as api_app


class SharedTechnicalIndicatorsTests(unittest.TestCase):
    def test_all_api_modules_import(self):
        self.assertIsNotNone(api_app)
        self.assertTrue(any(route.path == "/api/v1/crypto/okx/scan" for route in api_app.routes))

    def test_coinbase_and_okx_drop_forming_candle(self):
        closes = [float(i) for i in range(1, 61)]
        coinbase = [[i, closes[i-1]-1, closes[i-1]+1, closes[i-1], closes[i-1], 100.0] for i in range(1, 61)]
        okx = [[i, closes[i-1], closes[i-1]+1, closes[i-1]-1, closes[i-1], 100.0] for i in range(1, 61)]
        coinbase_result = exchange_indicators(list(reversed(coinbase)), "Coinbase")
        okx_result = exchange_indicators(list(reversed(okx)), "OKX")
        self.assertAlmostEqual(coinbase_result["price"], 59.0)
        self.assertAlmostEqual(okx_result["price"], 59.0)

    def test_wilder_rsi_and_atr_on_monotonic_series(self):
        closes = [float(i) for i in range(1, 61)]
        highs = [value + 1.0 for value in closes]
        lows = [value - 1.0 for value in closes]
        volumes = [100.0] * len(closes)

        result = calculate_indicators(highs, lows, closes, volumes)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["rsi_14"], 100.0, places=8)
        self.assertAlmostEqual(result["atr_14"], 2.0, places=8)
        self.assertGreater(result["ema20"], result["ema50"])
        self.assertEqual(result["volume_ratio"], 1.0)

    def test_flat_market_has_neutral_rsi(self):
        closes = [100.0] * 60
        highs = [101.0] * 60
        lows = [99.0] * 60

        result = calculate_indicators(highs, lows, closes)

        self.assertIsNotNone(result)
        self.assertEqual(result["rsi_14"], 50.0)
        self.assertAlmostEqual(result["atr_14"], 2.0, places=8)
        self.assertAlmostEqual(result["ema20"], 100.0, places=8)

    def test_requires_at_least_51_completed_closes(self):
        closes = [float(i) for i in range(1, 51)]
        result = calculate_indicators(
            [x + 1 for x in closes],
            [x - 1 for x in closes],
            closes,
        )
        self.assertIsNone(result)

    def test_score_is_bounded_and_exposes_shared_fields(self):
        closes = [float(i) for i in range(1, 61)]
        data = calculate_indicators(
            [x + 1 for x in closes],
            [x - 1 for x in closes],
            closes,
            [100.0] * 60,
        )
        score = score_indicators(data, 10_000_000)

        self.assertGreaterEqual(score["score"], 0)
        self.assertLessEqual(score["score"], 100)
        self.assertIn("rsi_14", score)
        self.assertIn("ema20", score)
        self.assertIn("atr_14", score)


if __name__ == "__main__":
    unittest.main()
